import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from translator.ui.web_app import Application
from translator.ui.web_updates import UpdateMonitor


class WebUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "web").mkdir()
        (self.root / "translator").mkdir()
        (self.root / "web" / "app.js").write_text("first", encoding="utf-8")
        (self.root / "translator" / "engine.py").write_text("value = 1", encoding="utf-8")
        self.monitor = UpdateMonitor(self.root)
        cfg = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        cfg["output_dir"] = str(self.root / "output")
        self.app = Application(cfg)
        self.app.updates = self.monitor

    def test_frontend_and_backend_changes_are_distinguished_and_new_baseline_resets(self):
        first = self.monitor.snapshot(force=True)
        (self.root / "web" / "app.js").write_text("second page", encoding="utf-8")
        changed = self.monitor.snapshot(force=True)
        self.assertNotEqual(first["frontend_revision"], changed["frontend_revision"])
        self.assertFalse(changed["backend_changed"])
        (self.root / "translator" / "new.py").write_text("value = 2", encoding="utf-8")
        self.assertTrue(self.monitor.snapshot(force=True)["backend_changed"])
        restarted = UpdateMonitor(self.root).snapshot(force=True)
        self.assertFalse(restarted["backend_changed"])
        self.assertNotEqual(first["instance"], restarted["instance"])

    def test_invalid_syntax_and_failed_import_leave_current_service_available(self):
        (self.root / "translator" / "engine.py").write_text("def broken(", encoding="utf-8")
        self.app.restart_callback = lambda: self.fail("must not restart")
        with self.assertRaisesRegex(ValueError, "检查失败"):
            self.app.request_restart("now")
        self.assertFalse(self.app.restart_requested)
        (self.root / "translator" / "engine.py").write_text("value = 1", encoding="utf-8")
        with patch("translator.ui.web_updates.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "bad import")):
            with self.assertRaisesRegex(ValueError, "bad import"):
                self.monitor.validate_backend()

    def test_wait_restart_preserves_tasks_and_waits_for_both_and_browser_release(self):
        called = threading.Event()
        self.app.restart_callback = called.set
        self.app.task["running"] = self.app.acquisition_task["running"] = True
        self.app._browser_owner = object()
        with patch.object(self.monitor, "validate_backend"):
            self.app.request_restart("wait")
            self.assertFalse(self.app.cancel.is_set())
            self.assertFalse(self.app.acquisition_cancel.is_set())
            self.assertFalse(called.wait(.3))
            self.app.task["running"] = False
            self.assertFalse(called.wait(.3))
            self.app.acquisition_task["running"] = False
            self.assertFalse(called.wait(.3))
            self.app._browser_owner = None
            self.assertTrue(called.wait(5))

    def test_stop_restart_sets_cancel_flags_but_does_not_restart_before_task_exit(self):
        called = threading.Event()
        self.app.restart_callback = called.set
        self.app.task["running"] = True
        with patch.object(self.monitor, "validate_backend"):
            self.app.request_restart("stop")
            self.assertTrue(self.app.cancel.is_set())
            self.assertTrue(self.app.acquisition_cancel.is_set())
            self.assertTrue(self.app.task["stopping"])
            self.assertFalse(called.wait(.3))
            self.app.task["running"] = False
            self.assertTrue(called.wait(5))

    def test_cancel_pending_restart_and_reject_new_jobs_while_waiting(self):
        called = threading.Event()
        self.app.restart_callback = called.set
        self.app.task["running"] = True
        with patch.object(self.monitor, "validate_backend"):
            self.app.request_restart("wait")
            with self.assertRaisesRegex(ValueError, "等待更新重启"):
                self.app.acquire({"url": "https://kakuyomu.jp/works/123"})
            with self.assertRaisesRegex(ValueError, "等待更新重启"):
                self.app.write_terms({"project": "book", "entries": []})
            self.app.cancel_restart()
            self.app.task["running"] = False
            self.assertFalse(called.wait(.3))
            self.assertFalse(self.app.restart_requested)

    def test_embedded_server_and_busy_immediate_restart_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "启动方式"):
            self.app.request_restart("now")
        self.app.restart_callback = lambda: None
        self.app.task["running"] = True
        with patch.object(self.monitor, "validate_backend"):
            with self.assertRaisesRegex(ValueError, "仍在运行"):
                self.app.request_restart("now")
