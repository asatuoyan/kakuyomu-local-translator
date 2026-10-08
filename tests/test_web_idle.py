import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from translator.ui.web_app import Application, create_server


class IdleCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = json.loads(Path('config.example.json').read_text(encoding='utf-8'))
        cfg['output_dir'] = self.tmp.name
        self.app = Application(cfg)
        self.clock = patch('translator.ui.web_idle.time.monotonic', return_value=100)
        self.now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def complete(self):
        self.app.finish_queue_task('translation', {}, 'Novel')
        return self.app.idle_close_status()

    def test_no_shutdown_before_a_task_completes_in_this_session(self):
        self.assertIsNone(self.app.idle_close_status()['remaining_seconds'])
        self.now.return_value = 10000
        self.assertFalse(self.app.idle_close_status(close_if_due=True)['closing'])

    def test_closes_after_one_hour_and_status_polling_does_not_reset_timer(self):
        self.assertEqual(self.complete()['remaining_seconds'], 3600)
        self.now.return_value = 3699
        self.assertEqual(self.app.status()['idle_close']['remaining_seconds'], 1)
        self.assertFalse(self.app.idle_close_status(close_if_due=True)['closing'])
        self.now.return_value = 3700
        self.assertTrue(self.app.idle_close_status(close_if_due=True)['closing'])

    def test_activity_resets_full_hour(self):
        self.complete()
        self.now.return_value = 3699
        self.app.note_activity()
        self.assertEqual(self.app.idle_close_status()['remaining_seconds'], 3600)
        self.now.return_value = 3700
        self.assertFalse(self.app.idle_close_status(close_if_due=True)['closing'])

    def test_pending_failed_paused_and_running_work_suspend_countdown(self):
        self.complete()
        for status in ('waiting', 'failed', 'paused'):
            self.app.queued_tasks = [{'status': status}]
            self.assertIsNone(self.app.idle_close_status()['remaining_seconds'])
        self.app.queued_tasks = []
        for task in (self.app.task, self.app.acquisition_task):
            for key in ('running', 'recovering', 'retry_available'):
                task[key] = True
                self.assertIsNone(self.app.idle_close_status()['remaining_seconds'])
                task[key] = False
        self.now.return_value = 10000
        self.assertEqual(self.app.idle_close_status()['remaining_seconds'], 3600)

    def test_recovery_launch_queue_errors_and_restart_suspend_countdown(self):
        self.complete()
        with patch.object(self.app.recovery, 'snapshot', return_value={'translation': {}}):
            self.assertIsNone(self.app.idle_close_status()['remaining_seconds'])
        for attribute, busy, empty in (('_queue_launching', {'translation'}, set()),
                                        ('queue_error', 'Unreadable queue', ''),
                                        ('restart_requested', True, False),
                                        ('_browser_owner', object(), None)):
            setattr(self.app, attribute, busy)
            self.assertIsNone(self.app.idle_close_status()['remaining_seconds'])
            setattr(self.app, attribute, empty)

    def test_user_can_cancel_and_reenable_with_a_fresh_hour(self):
        self.complete()
        self.app.control_idle_close(False)
        self.now.return_value = 10000
        self.assertFalse(self.app.idle_close_status(close_if_due=True)['closing'])
        self.assertEqual(self.app.control_idle_close(True)['remaining_seconds'], 3600)
        with self.assertRaises(ValueError):
            self.app.control_idle_close('true')

    def test_activity_endpoint_resets_timer_but_background_status_does_not(self):
        self.complete()
        server = create_server(self.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.now.return_value = 200
            with urlopen(server.url + 'api/status') as response:
                self.assertEqual(json.load(response)['idle_close']['remaining_seconds'], 3500)
            token = server.url.split('/')[-2]
            request = Request(server.url + 'api/activity', b'{}',
                              {'X-Local-App': token, 'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)['recorded'])
            self.assertEqual(self.app.idle_close_status()['remaining_seconds'], 3600)
        finally:
            server.shutdown()
            server.server_close()

    def test_reader_interaction_counts_but_catalog_polling_does_not(self):
        from translator.ui.web_reader import ReadingServer
        self.complete()
        reader = ReadingServer(activity_callback=self.app.note_activity)
        try:
            self.now.return_value = 200
            with urlopen(reader.url() + 'catalog') as response:
                json.load(response)
            self.assertEqual(self.app.idle_close_status()['remaining_seconds'], 3500)
            with urlopen(reader.url() + 'activity') as response:
                json.load(response)
            self.assertEqual(self.app.idle_close_status()['remaining_seconds'], 3600)
        finally:
            reader.close()
