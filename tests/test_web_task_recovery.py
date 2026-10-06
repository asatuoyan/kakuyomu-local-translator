import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from translator.engine import TranslationCancelled
from translator.ui.web_app import Application
from translator.ui.web_task_recovery import TaskJournal


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = json.loads(Path('config.example.json').read_text(encoding='utf-8'))
        self.cfg['output_dir'] = self.temp.name
        self.source = Path(self.temp.name) / 'novel.epub'
        self.source.write_bytes(b'original')
        self.body = {'source': str(self.source), 'language': 'en', 'model': 'saved-model'}
        self.apps = []
        self.addCleanup(self.close_apps)
        self.patches = [patch('translator.engine.ensure_model'), patch('translator.ui.web_app.update_config'),
                        patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book')]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def app(self):
        app = Application(dict(self.cfg))
        self.apps.append(app)
        return app

    def close_apps(self):
        for app in self.apps:
            app.closing.set()
            app.cancel.set()
            app.acquisition_cancel.set()
            if hasattr(app, '_recovery_thread'):
                app._recovery_thread.join(2)
        for app in self.apps:
            self.wait(lambda: not app.task['running'] and not app.acquisition_task['running'])

    def wait(self, predicate):
        limit = time.monotonic() + 5
        while not predicate():
            self.assertLess(time.monotonic(), limit)
            time.sleep(.01)

    def cancelled_translation(self, source, cfg, language, progress):
        self.assertEqual(source, self.source)
        self.assertEqual(language, 'en')
        self.assertEqual(cfg['model'], 'saved-model')
        while not cfg['_translation_cancelled']():
            time.sleep(.01)
        raise TranslationCancelled()

    def test_journal_is_durable_and_old_worker_cannot_clear_a_new_task(self):
        journal = TaskJournal(self.temp.name)
        old = journal.begin('translation', self.body)
        new = journal.begin('translation', {**self.body, 'model': 'new-model'})
        journal.clear('translation', old)
        reopened = TaskJournal(self.temp.name)
        self.assertTrue(reopened.matches('translation', new))
        self.assertEqual(reopened.snapshot()['translation']['body']['model'], 'new-model')

    def test_interrupted_task_is_restored_once_with_same_parameters(self):
        with patch('translator.engine.translate_epub_language', side_effect=self.cancelled_translation) as translate:
            first = self.app()
            first.start(self.body)
            first.cancel.set()
            self.wait(lambda: not first.task['running'])
            self.assertIn('translation', TaskJournal(self.temp.name).snapshot())
            second = self.app()
            second.restore_tasks()
            second.restore_tasks()
            self.wait(lambda: second.task['running'])
            second.stop_task('translation')
            self.wait(lambda: not second.task['running'])
            self.assertEqual(translate.call_count, 2)
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})

    def test_explicit_stop_does_not_resume_after_restart(self):
        with patch('translator.engine.translate_epub_language', side_effect=self.cancelled_translation) as translate:
            first = self.app()
            first.start(self.body)
            first.stop_task('translation')
            self.wait(lambda: not first.task['running'])
            second = self.app()
            second.restore_tasks()
            second._recovery_thread.join(2)
            self.assertEqual(translate.call_count, 1)
            self.assertFalse(second.task['running'])

    def test_finished_and_failed_tasks_are_removed(self):
        with patch('translator.engine.translate_epub_language', return_value=Path(self.temp.name) / 'done.epub'):
            app = self.app()
            app.start(self.body)
            self.wait(lambda: not app.task['running'])
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})
        with patch('translator.engine.translate_epub_language', side_effect=RuntimeError('model failure')):
            app.start(self.body)
            self.wait(lambda: not app.task['running'])
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})

    def test_translation_and_acquisition_resume_together_and_wait_for_browser(self):
        journal = TaskJournal(self.temp.name)
        journal.begin('translation', self.body)
        journal.begin('acquisition', {'url': 'https://kakuyomu.jp/works/789'})
        def acquire(url, cfg, *args, **kwargs):
            while not cfg['_translation_cancelled']():
                time.sleep(.01)
            raise TranslationCancelled()
        with patch('translator.engine.translate_epub_language', side_effect=self.cancelled_translation), \
             patch('translator.acquisition.network_workflow.acquire_source', side_effect=acquire) as download:
            app = self.app()
            owner = object()
            app._browser_owner = owner
            app.restore_tasks()
            self.wait(lambda: app.task['running'])
            self.assertTrue(app.acquisition_task['recovering'])
            download.assert_not_called()
            app._release_browser(owner)
            self.wait(lambda: app.acquisition_task['running'])
            self.assertTrue(app.task['running'])
            app.stop_task('translation')
            app.stop_task('acquisition')
            self.wait(lambda: not app.task['running'] and not app.acquisition_task['running'])
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})

    def test_recovery_can_be_cancelled_during_model_validation(self):
        TaskJournal(self.temp.name).begin('translation', self.body)
        entered, released = threading.Event(), threading.Event()
        def validate(*args, **kwargs):
            entered.set()
            self.assertTrue(released.wait(5))
        with patch('translator.engine.ensure_model', side_effect=validate), \
             patch('translator.engine.translate_epub_language') as translate:
            app = self.app()
            app.restore_tasks()
            self.assertTrue(entered.wait(5))
            app.stop_task('translation')
            released.set()
            app._recovery_thread.join(2)
            translate.assert_not_called()
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})

    def test_validation_failure_keeps_saved_task_and_reports_reason(self):
        TaskJournal(self.temp.name).begin('translation', self.body)
        with patch('translator.engine.ensure_model', side_effect=RuntimeError('Ollama unavailable')):
            app = self.app()
            app.restore_tasks()
            app._recovery_thread.join(2)
        self.assertFalse(app.task['running'])
        self.assertIn('Ollama unavailable', app.task['message'])
        self.assertEqual(app.queued_tasks[0]['status'], 'failed')
        self.assertEqual(app.queued_tasks[0]['body']['source'], self.body['source'])

    def test_unavailable_ollama_waits_for_explicit_retry_without_losing_the_task(self):
        from requests.exceptions import ConnectionError
        TaskJournal(self.temp.name).begin('translation', self.body)
        attempts = []
        def validate(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError('Ollama not started yet') from ConnectionError('offline')
        with patch('translator.engine.ensure_model', side_effect=validate), \
             patch('translator.engine.translate_epub_language', side_effect=self.cancelled_translation):
            app = self.app()
            app.restore_tasks()
            self.wait(lambda: bool(app.queued_tasks) and app.queued_tasks[0]['status'] == 'failed')
            time.sleep(.4)
            self.assertEqual(len(attempts), 1)
            app.change_queue({'id': app.queued_tasks[0]['id'], 'action': 'retry'})
            self.wait(lambda: app.task['running'])
            self.assertEqual(len(attempts), 2)
            app.stop_task('translation')
            self.wait(lambda: not app.task['running'])
            self.assertEqual(TaskJournal(self.temp.name).snapshot(), {})

    def test_corrupt_journal_does_not_prevent_service_start_or_overwrite_the_file(self):
        path = Path(self.temp.name) / 'web-active-tasks.json'
        path.write_text('{broken', encoding='utf-8')
        app = self.app()
        app.restore_tasks()
        app._recovery_thread.join(2)
        self.assertFalse(app.task['running'])
        self.assertTrue(app.recovery.error)
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
