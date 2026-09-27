"""Small GUI checks for theme changes and task controls."""

import tkinter as tk
import time
import unittest
from tkinter import ttk
from unittest.mock import patch

from gui import TranslatorGUI
from gui_settings import SettingsDialog


class GuiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.app = TranslatorGUI()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "app") and cls.app.winfo_exists():
            cls.app.destroy()

    def test_theme_change_updates_open_window_and_controls(self):
        dialog = tk.Toplevel(self.app)
        with patch.object(self.app, "_save_preferences"):
            self.app._toggle_theme()
        self.assertEqual(dialog.cget("background"), self.app.palette["background"])
        self.assertEqual(self.app.cget("background"), self.app.palette["background"])
        self.assertEqual(self.app.style.lookup("TFrame", "background"), self.app.palette["surface"])

    def test_busy_state_disables_task_buttons_and_restores_them(self):
        start_button = self.app.btn_start_tr
        self.app._set_busy(True, "正在处理")
        self.assertEqual(str(start_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.settings_button.cget("state")), "normal")
        self.app._set_busy(False)
        self.assertEqual(str(start_button.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")

    def test_worker_updates_progress_and_status_on_gui(self):
        self.app.log_queue.put(("progress", 42.0))
        self.app.log_queue.put(("status", "正在翻译"))
        self.app._poll_queue()
        self.assertEqual(self.app.progress_var.get(), 42.0)
        self.assertEqual(self.app.status_var.get(), "正在翻译")

    def test_settings_shows_theme_and_discovers_installed_model(self):
        with patch("gui_settings.installed_models", return_value={"local-translator:latest"}):
            dialog = SettingsDialog(self.app)
            deadline = time.monotonic() + 2
            while "local-translator:latest" not in dialog.model_box["values"] and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.02)
        self.assertIn("local-translator:latest", dialog.model_box["values"])
        self.assertIn("local-translator:latest", self.app.tr_model_box["values"])
        self.assertGreaterEqual(dialog.winfo_height(), dialog.minsize()[1])
        self.assertTrue(any(child.cget("text") == "界面主题" for child in dialog.winfo_children()[0].winfo_children()
                            if isinstance(child, ttk.Label)))
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
