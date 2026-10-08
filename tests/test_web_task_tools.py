import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from translator.engine import TranslationCancelled
from translator.storage.project_storage import atomic_json, load_json
from translator.ui.web_app import Application
from translator.ui.web_task_recovery import TaskJournal


class TaskToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = json.loads(Path('config.example.json').read_text(encoding='utf-8'))
        self.cfg['output_dir'] = self.temp.name
        self.source = Path(self.temp.name) / 'source.epub'
        self.source.write_bytes(b'fixture')
        self.body = {'source': str(self.source), 'language': 'en', 'model': 'fixture'}
        self.apps = []
        self.addCleanup(self.close)
        for target in ('translator.engine.ensure_model', 'translator.ui.web_app.update_config'):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)

    def app(self):
        app = Application(dict(self.cfg))
        self.apps.append(app)
        return app

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)

    def close(self):
        for app in self.apps:
            app.closing.set()
            app.cancel.set()
            app.acquisition_cancel.set()
        for app in self.apps:
            for thread in [getattr(app, '_recovery_thread', None), app._queue_thread, *app._queue_launch_threads]:
                if thread is not None:
                    thread.join(2)
            self.wait(lambda: not app.task['running'] and not app.acquisition_task['running'])

    def test_translation_metadata_replaces_internal_source_filename(self):
        app = self.app()
        app.task['title'] = 'source_598ceb02c855e49f'
        app.metadata_ready({'title': '作品书名'})
        self.assertEqual(app.task['title'], '作品书名')
        app.metadata_ready({'title': ' '})
        self.assertEqual(app.task['title'], '作品书名')

    def test_fifo_serializes_translation_and_cancel_removes_waiting_book(self):
        app = self.app()
        released = threading.Event()
        calls = []
        def translate(source, cfg, language, progress):
            calls.append(language)
            while not released.wait(.01):
                if cfg['_translation_cancelled']():
                    raise TranslationCancelled()
            return Path(self.temp.name) / 'translated.epub'
        with patch('translator.engine.translate_epub_language', side_effect=translate), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            first = app.enqueue('translation', self.body)
            self.wait(lambda: app.task['running'])
            app.enqueue('translation', {**self.body, 'language': 'zh-Hans'})
            third = app.enqueue('translation', {**self.body, 'language': 'zh-Hant'})
            app.change_queue({'id': third['id'], 'action': 'cancel'})
            self.assertEqual(calls, ['en'])
            self.assertNotIn(first['id'], [entry['id'] for entry in app.queued_tasks])
            released.set()
            self.wait(lambda: len(calls) == 2 and not app.task['running'])
            self.assertEqual(calls, ['en', 'zh-Hans'])
            self.assertEqual(app.queued_tasks, [])

    def test_single_book_pause_allows_next_book_and_explicit_resume(self):
        app = self.app()
        calls = []
        def translate(source, cfg, language, progress):
            calls.append(language)
            if len(calls) == 1:
                while not cfg['_translation_cancelled']():
                    time.sleep(.01)
                raise TranslationCancelled()
            return Path(self.temp.name) / 'translated.epub'
        with patch('translator.engine.translate_epub_language', side_effect=translate), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            first = app.enqueue('translation', self.body)
            self.wait(lambda: bool(calls))
            app.enqueue('translation', {**self.body, 'language': 'zh-Hans'})
            app.pause_task('translation')
            self.wait(lambda: calls == ['en', 'zh-Hans'] and not app.task['running'])
            self.assertEqual(app.queued_tasks[0]['id'], first['id'])
            self.assertEqual(app.queued_tasks[0]['status'], 'paused')
            self.assertEqual(app.recovery.snapshot(), {})
            app.change_queue({'id': first['id'], 'action': 'resume'})
            self.wait(lambda: len(calls) == 3 and not app.task['running'])
            self.assertEqual(calls, ['en', 'zh-Hans', 'en'])
            self.assertEqual(app.queued_tasks, [])

    def test_duplicate_identity_ignores_model_and_title_but_keeps_languages_separate(self):
        app = self.app()
        app.control_queue(True)
        first = app.enqueue('translation', self.body)
        duplicate = app.enqueue('translation', {**self.body, 'model': 'other', 'title': 'other title'})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['id'], first['id'])
        app.enqueue('translation', {**self.body, 'language': 'zh-Hans'})
        self.assertEqual(len(app.queued_tasks), 2)
        app.recovery.begin('acquisition', {'url': 'https://kakuyomu.jp/works/123'})
        duplicate = app.enqueue('acquisition', {'url': 'https://kakuyomu.jp/works/123', 'title': 'different'})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['slot'], 'acquisition')

    def test_original_url_and_cached_epub_share_active_translation_identity(self):
        app = self.app()
        url = 'https://kakuyomu.jp/works/123'
        app.recovery.begin('translation', {**self.body, '_book_identity': url})
        duplicate = app.enqueue('translation', {'url': url, 'language': 'en', 'model': 'other'})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['slot'], 'translation')
        self.assertEqual(app.queued_tasks, [])

    def test_followup_key_is_saved_when_the_book_is_already_queued(self):
        app = self.app()
        app.control_queue(True)
        first = app.enqueue('translation', self.body)
        duplicate = app.enqueue('translation', {**self.body, 'dedupe_key': 'followup-existing'})
        self.assertTrue(duplicate['duplicate'])
        app.change_queue({'id': first['id'], 'action': 'cancel'})
        restored = self.app()
        duplicate = restored.enqueue('translation', {**self.body, 'dedupe_key': 'followup-existing'})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(restored.queued_tasks, [])

    def test_priority_moves_waiting_book_ahead_without_changing_paused_state(self):
        app = self.app()
        app.control_queue(True)
        first = app.enqueue('translation', self.body)
        second = app.enqueue('translation', {**self.body, 'language': 'zh-Hans'})
        app.change_queue({'id': second['id'], 'action': 'priority'})
        self.assertEqual([item['id'] for item in app.queued_tasks], [second['id'], first['id']])
        app.change_queue({'id': second['id'], 'action': 'pause'})
        with self.assertRaises(ValueError):
            app.change_queue({'id': second['id'], 'action': 'priority'})
        restored = self.app()
        self.assertEqual(restored.queued_tasks[0]['status'], 'paused')

    def test_interrupted_single_pause_transfer_does_not_auto_resume_on_restart(self):
        journal = TaskJournal(self.temp.name)
        journal.begin('translation', {**self.body, '_queue_id': 'held'})
        atomic_json(Path(self.temp.name) / 'web-task-queue.json', {
            'version': 2, 'tasks': [{'id': 'held', 'slot': 'translation', 'body': self.body,
                                   'title': 'fixture', 'status': 'paused'}]})
        app = self.app()
        self.assertEqual(app.recovery.snapshot(), {})
        self.assertEqual(app.queued_tasks[0]['status'], 'paused')
        app.restore_tasks()
        time.sleep(.35)
        self.assertFalse(app.task['running'])

    def test_failed_recovery_does_not_block_the_next_book(self):
        app = self.app()
        app.recovery.begin('translation', {**self.body, 'source': str(Path(self.temp.name) / 'missing.epub')})
        app.restore_tasks()
        self.wait(lambda: bool(app.queued_tasks))
        with patch('translator.engine.translate_epub_language', return_value=Path(self.temp.name) / 'translated.epub') as translate, \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            app.enqueue('translation', self.body)
            self.wait(lambda: translate.called and not app.task['running'])
        self.assertEqual(len(app.queued_tasks), 1)
        self.assertEqual(app.queued_tasks[0]['status'], 'failed')
        self.assertEqual(app.recovery.snapshot(), {})

    def test_completed_history_retains_only_five_entries_across_restart(self):
        app = self.app()
        for index in range(8):
            app.finish_queue_task('translation', {}, str(index), 'book')
        self.assertEqual([item['title'] for item in app.completed_tasks], ['3', '4', '5', '6', '7'])
        restored = self.app()
        self.assertEqual(restored.completed_tasks, app.completed_tasks)

    def test_stale_pause_request_cannot_pause_a_different_active_book(self):
        app = self.app()
        current = app.recovery.begin('translation', self.body)
        with self.assertRaises(ValueError):
            app.pause_task('translation', 'stale-job')
        self.assertTrue(app.recovery.matches('translation', current))
        self.assertEqual(app.queued_tasks, [])
        app.pause_task('translation', current)
        self.assertEqual(app.queued_tasks[0]['status'], 'paused')
        self.assertEqual(app.recovery.snapshot(), {})

    def test_acquisition_pause_retains_incremental_translation_followup(self):
        app = self.app()
        body = {'url': 'https://kakuyomu.jp/works/123', 'updates_only': True,
                'known_chapters': 2, 'translate_after': {'language': 'en', 'model': 'fixture'},
                'followup_key': 'followup', 'title': 'fixture'}
        job = app.recovery.begin('acquisition', body)
        app.pause_task('acquisition', job)
        self.assertEqual(app.queued_tasks[0]['body'], body)
        self.assertEqual(app.queued_tasks[0]['status'], 'paused')
        restored = self.app()
        self.assertEqual(restored.queued_tasks, app.queued_tasks)

    def test_preflight_is_outside_application_lock_and_can_be_cancelled(self):
        entered, release = threading.Event(), threading.Event()
        app = self.app()
        def ensure(*args, **kwargs):
            entered.set()
            release.wait(3)
        with patch('translator.engine.ensure_model', side_effect=ensure), \
                patch('translator.engine.translate_epub_language') as translate:
            item = app.enqueue('translation', self.body)
            self.assertTrue(entered.wait(2))
            self.assertTrue(app.lock.acquire(timeout=.2))
            app.lock.release()
            app.change_queue({'id': item['id'], 'action': 'cancel'})
            release.set()
            self.wait(lambda: not app._queue_launching)
            translate.assert_not_called()
            self.assertEqual(app.recovery.snapshot(), {})

    def test_saved_queue_skips_task_already_transferred_to_active_journal(self):
        journal = TaskJournal(self.temp.name)
        journal.begin('translation', {**self.body, '_queue_id': 'first'})
        atomic_json(Path(self.temp.name) / 'web-task-queue.json', [
            {'id': 'first', 'slot': 'translation', 'body': self.body, 'status': 'waiting'},
            {'id': 'second', 'slot': 'translation', 'body': self.body, 'status': 'waiting'}])
        app = self.app()
        self.assertEqual([item['id'] for item in app.queued_tasks], ['second'])

    def test_missing_source_recovery_can_be_retried_after_file_is_restored(self):
        app = self.app()
        app.recovery.begin('translation', self.body)
        self.source.unlink()
        app.restore_tasks()
        self.wait(lambda: app.task.get('retry_available') and not app.task.get('recovering'))
        self.assertEqual(app.task['recovery_error']['code'], 'source_missing')
        self.source.write_bytes(b'restored')
        release = threading.Event()
        def translate(source, cfg, language, progress):
            release.wait(2)
            return Path(self.temp.name) / 'translated.epub'
        with patch('translator.engine.translate_epub_language', side_effect=translate), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            app.retry_recovery('translation')
            self.wait(lambda: app.task['running'])
            release.set()
            self.wait(lambda: not app.task['running'])
            self.assertEqual(app.recovery.snapshot(), {})

    def test_backups_are_bounded_and_corrupt_file_can_be_restored(self):
        app = self.app()
        folder = Path(self.temp.name) / 'book'
        path = folder / 'glossary.json'
        for number in range(5):
            atomic_json(path, [{'source': 'name', 'target': str(number)}])
        rows = app.backups({'project': 'book'})['backups']
        self.assertEqual(len(rows), 3)
        self.assertEqual(app.backups({})['projects'], ['book'])
        path.write_text('{corrupted', encoding='utf-8')
        app.restore_backup({'project': 'book', 'file': rows[0]['file'], 'id': rows[0]['id']})
        self.assertEqual(load_json(path, []), [{'source': 'name', 'target': '3'}])
        self.assertEqual(len(app.backups({'project': 'book'})['backups']), 3)
        with self.assertRaises(ValueError):
            app.restore_backup({'project': 'book', 'file': '../outside.json', 'id': rows[0]['id']})
        app.task['running'] = True
        try:
            with self.assertRaises(ValueError):
                app.restore_backup({'project': 'book', 'file': rows[0]['file'], 'id': rows[0]['id']})
        finally:
            app.task['running'] = False

    def test_login_failure_shows_specific_action_and_saved_parameters(self):
        app = self.app()
        app.record_task_failure('acquisition', {'url': 'https://kakuyomu.jp/works/123'}, ValueError('登录窗口已关闭，尚未完成登录'))
        self.assertEqual(app.acquisition_task['recovery_error']['code'], 'login_required')
        self.assertTrue(app.acquisition_task['retry_available'])

    def test_runtime_failure_remains_in_queue_and_can_be_retried(self):
        app = self.app()
        with patch('translator.engine.translate_epub_language', side_effect=RuntimeError('model not found')):
            item = app.enqueue('translation', self.body)
            self.wait(lambda: any(entry.get('status') == 'failed' for entry in app.queued_tasks)
                      and not app.task['running'])
        self.assertEqual(app.queued_tasks[0]['id'], item['id'])
        self.assertEqual(app.queued_tasks[0]['error']['code'], 'model_missing')
        self.assertFalse(app.task['retry_available'])
        with patch('translator.engine.translate_epub_language', return_value=Path(self.temp.name) / 'output.epub'), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            app.change_queue({'id': item['id'], 'action': 'retry'})
            self.wait(lambda: not app.queued_tasks and not app.task['running'])
            self.assertEqual(app.task['percent'], 100)

    def test_active_queued_task_restores_without_a_remaining_queue_entry(self):
        app = self.app()
        app.recovery.begin('translation', {**self.body, '_queue_id': 'already-transferred'})
        with patch('translator.engine.translate_epub_language', return_value=Path(self.temp.name) / 'output.epub'), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book') as work:
            app.restore_tasks()
            self.wait(lambda: app.task['percent'] == 100 and not app.task['running'])
            self.assertEqual(work.call_count, 1)
            self.assertEqual(app.recovery.snapshot(), {})

    def test_pause_persists_and_continue_resumes_active_job_before_waiting_books(self):
        calls = []
        def translate(source, cfg, language, progress):
            calls.append(language)
            if len(calls) == 1:
                while not cfg['_translation_cancelled']():
                    time.sleep(.01)
                raise TranslationCancelled()
            return Path(self.temp.name) / 'output.epub'
        with patch('translator.engine.translate_epub_language', side_effect=translate), \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            first = self.app()
            first.enqueue('translation', self.body)
            self.wait(lambda: first.task['running'])
            first.enqueue('translation', {**self.body, 'language': 'zh-Hans'})
            first.control_queue(True)
            self.wait(lambda: not first.task['running'])
            self.assertEqual(calls, ['en'])
            second = self.app()
            self.assertTrue(second.queue_paused)
            second.restore_tasks()
            self.assertFalse(second.task['running'])
            self.assertEqual(calls, ['en'])
            second.control_queue(False)
            self.wait(lambda: len(calls) == 3 and not second.task['running'])
            self.assertEqual(calls, ['en', 'en', 'zh-Hans'])

    def test_pause_during_model_preflight_leaves_job_waiting(self):
        entered, release = threading.Event(), threading.Event()
        def ensure(*args, **kwargs):
            entered.set()
            release.wait(3)
        app = self.app()
        with patch('translator.engine.ensure_model', side_effect=ensure), \
                patch('translator.engine.translate_epub_language') as translate:
            app.enqueue('translation', self.body)
            self.assertTrue(entered.wait(2))
            app.control_queue(True)
            release.set()
            self.wait(lambda: not app._queue_launching)
            translate.assert_not_called()
            self.assertEqual(len(app.queued_tasks), 1)
            self.assertEqual(app.queued_tasks[0]['status'], 'waiting')

    def test_failed_recovery_can_change_source_and_model_or_be_cancelled(self):
        app = self.app()
        missing = {**self.body, 'source': str(Path(self.temp.name) / 'missing.epub')}
        app.recovery.begin('translation', missing)
        app.restore_tasks()
        self.wait(lambda: app.task.get('retry_available'))
        app.control_queue(True)
        app.retry_recovery('translation', {'source': str(self.source), 'model': 'replacement'})
        saved = app.queued_tasks[0]['body']
        self.assertEqual(saved['source'], str(self.source))
        self.assertEqual(saved['model'], 'replacement')
        app.stop_task('translation')
        self.assertEqual(app.recovery.snapshot(), {})
        self.assertFalse(app.task.get('retry_available'))

    def test_diagnostics_allowlist_never_exports_credentials_inputs_or_arbitrary_errors(self):
        app = self.app()
        secret = 'BOOK_TEXT_AND_CREDENTIAL_SENTINEL'
        app.task.update(message=secret, source=secret, url=secret, project=secret,
            stages={'translation': secret}, counts={'acquired': secret, 'translated': 2, 'total': 5},
            retry_body={'password': secret}, recovery_error={'code': 'source_missing', 'detail': secret, 'advice': secret})
        app._model_stage = secret
        app.queued_tasks = [{'id': secret, 'slot': 'translation', 'status': 'failed',
                             'title': secret, 'body': {'cookies': secret}, 'error': {'code': secret, 'detail': secret}}]
        app.diagnostic_event('translation', {'code': secret})
        report = app.diagnostics()
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report['tasks']['translation']['counts']['translated'], 2)
        self.assertEqual(report['tasks']['translation']['error_code'], 'source_missing')
        self.assertIn('application_version', report)

    def test_followup_deduplication_survives_consumption_and_restart(self):
        app = self.app()
        app.control_queue(True)
        body = {**self.body, 'dedupe_key': 'stable-followup'}
        first = app.enqueue('translation', body)
        app.change_queue({'id': first['id'], 'action': 'cancel'})
        reopened = self.app()
        result = reopened.enqueue('translation', body)
        self.assertTrue(result['duplicate'])
        self.assertEqual(reopened.queued_tasks, [])

    def test_update_check_queues_new_translation_only_after_acquisition(self):
        from translator.acquisition.network_workflow import network_path
        from translator.acquisition.source_epub import WorkInfo
        app = self.app()
        app.control_queue(True)
        url = 'https://kakuyomu.jp/works/123'
        state_path = network_path(url, app.cfg)
        atomic_json(state_path, {'url': url, 'source': str(self.source), 'work': {'title': 'fixture'},
            'acquired_chapters': 2, 'total_chapters': 2, 'acquisition_complete': True})
        book = app.projects()[0]
        result = app.check_book_updates({'project': book['id'], 'model': 'fixture', 'language': 'en'})
        saved = app.queued_tasks[0]['body']
        self.assertTrue(saved['updates_only'])
        self.assertEqual(saved['known_chapters'], 2)
        work = WorkInfo(url, 'fixture', '', '', '', [{'url': f'{url}/episodes/{i}', 'title': str(i)} for i in range(3)])
        with patch('translator.acquisition.network_workflow.acquire_source', return_value=(self.source, work)) as acquire, \
                patch('translator.engine.translate_epub_language', return_value=Path(self.temp.name) / 'output.epub') as translate, \
                patch('translator.engine.translation_work_dir', return_value=Path(self.temp.name) / 'book'):
            app.control_queue(False)
            self.wait(lambda: translate.called and not app.task['running'] and not app.acquisition_task['running'])
            self.assertTrue(acquire.call_args.kwargs['updates_only'])
            self.assertEqual(app.acquisition_task['new_chapters'], 1)
            self.assertEqual(translate.call_args.args[2], 'en')
            self.assertIn(saved['followup_key'], app._queue_keys)
            self.assertEqual(app.queued_tasks, [])

    def test_update_check_without_new_chapters_never_queues_translation(self):
        from translator.acquisition.network_workflow import network_path
        from translator.acquisition.source_epub import WorkInfo
        app = self.app()
        app.control_queue(True)
        url = 'https://kakuyomu.jp/works/123'
        atomic_json(network_path(url, app.cfg), {'url': url, 'source': str(self.source), 'work': {'title': 'fixture'},
            'acquired_chapters': 2, 'total_chapters': 2, 'acquisition_complete': True})
        book = app.projects()[0]
        app.check_book_updates({'project': book['id'], 'model': 'fixture', 'language': 'en'})
        work = WorkInfo(url, 'fixture', '', '', '', [{'url': f'{url}/episodes/{i}', 'title': str(i)} for i in range(2)])
        with patch('translator.acquisition.network_workflow.acquire_source', return_value=(self.source, work)), \
                patch('translator.engine.translate_epub_language') as translate:
            app.control_queue(False)
            self.wait(lambda: app.acquisition_task.get('percent') == 100 and not app.acquisition_task['running'])
            self.assertEqual(app.acquisition_task['new_chapters'], 0)
            translate.assert_not_called()
            self.assertEqual(app.queued_tasks, [])

    def test_reselecting_renamed_original_preserves_existing_translation_project(self):
        from translator.acquisition.source_epub import SourceChapter, translated_source_epub
        from translator.engine import translate_epub_language, translation_work_dir
        from translator.storage.project_storage import load_translation_state
        translated_source_epub({'title': 'fixture', 'author': 'writer'},
            [SourceChapter('epub://one', 'chapter', ['original text'], blocks=[{'type': 'text', 'text': 'original text'}])], self.source, 'en')
        cfg = {**self.cfg, 'model': 'fixture', 'adaptive_translation_batches': False, 'check_glossary': False}
        with patch('translator.engine.translate_episode', side_effect=lambda ep, *_: ['translated ' + p for p in ep.paragraphs]):
            translate_epub_language(self.source, cfg, 'en')
        folder = translation_work_dir(self.source, cfg, 'en')
        previous = load_translation_state(folder / 'translation-project.json')['chapters']
        renamed = Path(self.temp.name) / 'renamed.epub'
        renamed.write_bytes(self.source.read_bytes())
        self.source.unlink()
        app = self.app()
        updated = app.recovery_settings('translation', self.body, {'source': str(renamed), 'model': 'replacement'})
        self.assertEqual(updated['_resume_project'], str(folder.relative_to(Path(self.temp.name))))
        cfg.update(_resume_work_dir=str(folder), model='replacement')
        with patch('translator.engine.translate_episode', side_effect=lambda ep, *_: ['translated ' + p for p in ep.paragraphs]):
            translate_epub_language(renamed, cfg, 'en')
        restored = load_translation_state(folder / 'translation-project.json')
        self.assertEqual(restored['chapters'], previous)
        self.assertEqual(restored['source_path'], str(renamed))

    def test_web_file_selection_cancels_cleanly_and_never_uses_console_input(self):
        from translator.ui import file_dialogs
        with patch('builtins.input') as console:
            with patch.object(file_dialogs, '_run_dialog', return_value=''):
                self.assertTrue(file_dialogs.choose_web_source()['cancelled'])
            with patch.object(file_dialogs, '_run_dialog', return_value=file_dialogs._UNAVAILABLE):
                with self.assertRaisesRegex(ValueError, '完整路径'):
                    file_dialogs.choose_web_source()
            with patch.object(file_dialogs, '_run_dialog', return_value=str(self.source)):
                self.assertEqual(file_dialogs.choose_web_source()['source'], str(self.source))
            console.assert_not_called()


if __name__ == '__main__':
    unittest.main()
