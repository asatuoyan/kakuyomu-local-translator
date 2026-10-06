"""Small GUI checks for theme changes and task controls."""

import tkinter as tk
import time
import unittest
from tkinter import ttk
from unittest.mock import patch

from translator.ui.gui import TranslatorGUI, TextRedirector
from translator.ui.gui_settings import SettingsDialog


class GuiStateTests(unittest.TestCase):
    def test_network_workflow_starts_without_manual_catalog_or_epub_selection(self):
        old_model = self.app.tr_model_var.get()
        old_url = self.app.dl_url_var.get()
        old_running = getattr(self.app, "workflow_running", False)
        old_stop = self.app.stop_requested
        try:
            self.app.tr_model_var.set("test-model")
            self.app.dl_url_var.set("https://kakuyomu.jp/works/123")
            with patch("translator.ui.gui_workflow.threading.Thread") as thread:
                self.app._action_network_workflow()
            thread.return_value.start.assert_called_once()
            self.assertTrue(self.app.task_busy)
            self.assertEqual(str(self.app.btn_stop.cget("state")), "normal")
        finally:
            self.app.workflow_running = old_running
            self.app.stop_requested = old_stop
            self.app._set_busy(False)
            self.app.tr_model_var.set(old_model)
            self.app.dl_url_var.set(old_url)

    def test_full_confirmation_on_translation_page_dispatches_online_acquisition(self):
        previous = getattr(self.app, "workflow_state", {})
        try:
            self.app.workflow_state = {"network_url": "https://kakuyomu.jp/works/123"}
            with patch.object(self.app, "_sync_workflow"), patch.object(self.app, "_action_network_workflow") as run:
                self.app._action_start_translate(preview=False)
            run.assert_called_once_with(full=True, url="https://kakuyomu.jp/works/123")
        finally:
            self.app.workflow_state = previous

    def test_workflow_restores_preview_gate_and_disables_full_for_another_book(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from translator.storage.project_storage import atomic_json
        from translator.translation.translation_workflow import workflow_path
        old_cfg = self.app.cfg
        old_source = self.app.tr_epub_var.get()
        old_languages = {code: var.get() for code, var in self.app.tr_language_vars.items()}
        try:
            with TemporaryDirectory() as directory:
                source = Path(directory) / "book.epub"
                source.write_bytes(b"source")
                self.app.cfg = {**old_cfg, "output_dir": directory}
                for code, var in self.app.tr_language_vars.items():
                    var.set(code == "zh-Hans")
                self.app.tr_epub_var.set(str(source))
                self.assertEqual(str(self.app.btn_full_tr.cget("state")), "disabled")
                atomic_json(workflow_path(source, self.app.cfg), {
                    "source": str(source.resolve()), "stage": "awaiting_confirmation",
                    "previews": {"zh-Hans": {"chapters": 20, "issues": 2}}})
                self.app._sync_workflow()
                self.assertEqual(str(self.app.btn_full_tr.cget("state")), "normal")
                self.app._set_busy(True, "试译", cancellable=True)
                self.assertEqual(str(self.app.btn_full_tr.cget("state")), "disabled")
                self.app._set_busy(False)
                self.assertEqual(str(self.app.btn_full_tr.cget("state")), "normal")
                self.app.tr_epub_var.set(str(Path(directory) / "other.epub"))
                self.assertEqual(str(self.app.btn_full_tr.cget("state")), "disabled")
        finally:
            self.app.cfg = old_cfg
            self.app.tr_epub_var.set(old_source)
            for code, value in old_languages.items():
                self.app.tr_language_vars[code].set(value)
            self.app._set_busy(False)

    def test_download_button_remains_inside_default_and_minimum_window(self):
        self.app.deiconify()
        self.addCleanup(self.app.withdraw)
        self.app._select_page("download")
        for size in ("1120x760", "980x680"):
            with self.subTest(size=size):
                self.app.geometry(size)
                self.app.update()
                button = self.app.btn_start_dl
                page = self.app.tab_download
                self.assertTrue(button.winfo_viewable())
                self.assertGreater(button.winfo_height(), 20)
                self.assertGreaterEqual(button.winfo_rooty(), page.winfo_rooty())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                     page.winfo_rooty() + page.winfo_height())
                for action in (self.app.btn_network_preview, self.app.btn_network_full):
                    self.assertTrue(action.winfo_viewable())
                    self.assertLessEqual(action.winfo_rooty() + action.winfo_height(),
                                         page.winfo_rooty() + page.winfo_height())

    def test_new_download_resets_previous_stop_request(self):
        from translator.acquisition.source_epub import WorkInfo
        previous_work = self.app.current_work
        previous_stop = self.app.stop_requested
        try:
            self.app.current_work = WorkInfo("https://kakuyomu.jp/works/1", "测试", "", "", "",
                                             [{"url": "https://kakuyomu.jp/works/1/episodes/2", "title": "第一章"}])
            self.app.dl_start_var.set("1")
            self.app.dl_end_var.set("1")
            self.app.stop_requested = True
            with patch("translator.ui.gui.threading.Thread") as worker:
                self.app._action_start_download()
            self.assertFalse(self.app.stop_requested)
            worker.return_value.start.assert_called_once()
            self.assertEqual(str(self.app.btn_stop.cget("state")), "normal")
            self.app.btn_stop.invoke()
            self.assertTrue(self.app.stop_requested)
        finally:
            self.app.current_work = previous_work
            self.app.stop_requested = previous_stop
            self.app._set_busy(False)

    def test_stop_is_enabled_for_cancellable_tasks_and_disabled_when_finished(self):
        previous_stop = self.app.stop_requested
        try:
            self.app.stop_requested = False
            self.app._set_busy(True, "翻译中", cancellable=True)
            self.assertEqual(str(self.app.btn_start_tr.cget("state")), "disabled")
            self.assertEqual(str(self.app.btn_stop.cget("state")), "normal")
            self.app.btn_stop.invoke()
            self.assertTrue(self.app.stop_requested)
            self.app._set_busy(False)
            self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")
            self.app._set_busy(True, "读取目录")
            self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")
        finally:
            self.app.stop_requested = previous_stop
            self.app._set_busy(False)

    def test_open_saved_reading_replaces_live_content_and_opens_browser(self):
        from types import SimpleNamespace
        saved = SimpleNamespace(identity="saved-book", title="书名")
        self.app.reading_chapters[("旧书", 9)] = ("旧章节", [], [])
        with patch("translator.ui.gui.filedialog.askopenfilename", return_value="finished.epub"), \
                patch("translator.reading.saved_reading.SavedReading", return_value=saved), \
                patch.object(self.app, "_open_web_reader") as open_reader:
            self.app._open_saved_reading()
            deadline = time.monotonic() + 3
            while self.app.saved_reading_loading and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.01)
            open_reader.assert_called_once()
        self.assertEqual(self.app.reading_book, "saved-book")
        self.assertEqual(self.app.reading_chapters, {})
        self.assertIs(self.app.web_reader_server.saved, saved)
        self.app.web_reader_server.reset("")

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
        self.assertEqual(self.app.progress_text_var.get(), "42.0%")
        self.assertEqual(self.app.status_var.get(), "正在翻译")

    def test_status_is_simplified_before_widgets_observe_updates(self):
        seen = []
        trace = self.app.status_var.trace_add("write", lambda *_: seen.append(self.app.status_var.get()))
        try:
            self.app.log_queue.put(("status", "簡體中文 · 章節 2/251"))
            self.app._poll_queue()
            self.assertEqual(seen, ["简体中文 · 章节 2/251"])
            self.app.log_queue.put(("status", "繁體中文 · 章節 3/251"))
            self.app._poll_queue()
            self.assertEqual(seen[-1], "繁体中文 · 章节 3/251")
        finally:
            self.app.status_var.trace_remove("write", trace)

    def test_log_window_does_not_shrink_translation_page(self):
        self.app.deiconify()
        self.addCleanup(self.app.withdraw)
        self.app._select_page("translate")
        self.app.geometry("980x680")
        self.app.update()
        height = self.app.tab_translate.winfo_height()
        self.app._toggle_log()
        self.app.update()
        try:
            self.assertEqual(self.app.tab_translate.winfo_height(), height)
            self.assertTrue(self.app.btn_start_tr.winfo_viewable())
            self.assertTrue(self.app.log_window.winfo_viewable())
        finally:
            self.app._toggle_log()

    def test_log_capture_preserves_text_and_manual_scroll(self):
        self.app.log_window.deiconify()
        self.addCleanup(self.app.log_window.withdraw)
        self.app._append_log("\n".join(f"line {i}" for i in range(200)) + "\n")
        self.app.update()
        self.app.log_text.yview_moveto(0)
        self.app.update()
        before = self.app.log_text.index("@0,0")
        redirector = TextRedirector(self.app.log_text, self.app.log_queue)
        redirector.write("批次 1/2 · C:/小說/原文.epub\n")
        self.app._poll_queue()
        self.app.update()
        self.assertEqual(self.app.log_text.index("@0,0"), before)
        self.assertIn("C:/小說/原文.epub", self.app.log_text.get("1.0", "end"))
        self.assertEqual(str(self.app.log_text.cget("state")), "disabled")

    def test_translation_progress_is_visible_at_minimum_window_size(self):
        self.app.deiconify()
        self.addCleanup(self.app.withdraw)
        self.app._select_page("translate")
        self.app.geometry("980x680")
        self.app.update()
        panel = self.app.tr_progress_frame
        self.assertEqual(panel.winfo_manager(), "pack")
        self.assertEqual(str(self.app.tr_progress_bar.cget("variable")),
                         str(self.app.progress_var))
        self.assertGreater(panel.winfo_height(), 50)
        self.assertLessEqual(panel.winfo_rooty() + panel.winfo_height(),
                             self.app.winfo_rooty() + self.app.winfo_height())
        for widget in (self.app.btn_start_tr, self.app.btn_full_tr, self.app.tr_model_box,
                       self.app.tr_bilingual_check):
            self.assertTrue(widget.winfo_viewable())
            self.assertGreater(widget.winfo_height(), 20)
            self.assertGreaterEqual(widget.winfo_rooty(), self.app.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.app.winfo_rooty() + self.app.winfo_height())

    def test_translation_page_does_not_duplicate_footer_progress(self):
        self.app.deiconify()
        self.addCleanup(self.app.withdraw)
        self.app._select_page("translate")
        self.app.update()
        for widget in (self.app.status_bar, self.app.footer_progress_text,
                       self.app.progress_bar):
            self.assertFalse(widget.winfo_ismapped())
        for widget in (self.app.tr_progress_frame, self.app.log_toggle, self.app.btn_stop):
            self.assertTrue(widget.winfo_viewable())
        self.app._select_page("download")
        self.app.update()
        for widget in (self.app.status_bar, self.app.footer_progress_text,
                       self.app.progress_bar):
            self.assertTrue(widget.winfo_viewable())
        self.app._select_page("translate")
        self.app.update()
        self.assertFalse(self.app.progress_bar.winfo_ismapped())

    def test_phone_reading_addresses_survive_dialog_creation(self):
        import gc
        addresses = ["http://192.168.1.10:12345/token/", "http://192.168.1.11:12345/token/"]
        with patch("translator.ui.web_reader.ReadingServer.lan_urls", return_value=addresses), patch("webbrowser.open"):
            self.app._open_web_reader()
        dialog = next(child for child in self.app.winfo_children()
                      if isinstance(child, tk.Toplevel) and child.title() == "网页 / 手机阅读")
        self.addCleanup(dialog.destroy)
        gc.collect()
        self.app.update()
        entries = [child for child in dialog.winfo_children()[0].winfo_children()
                   if isinstance(child, ttk.Entry)]
        self.assertEqual([entry.get() for entry in entries], addresses)
        self.assertTrue(all(str(entry.cget("state")) == "readonly" for entry in entries))

    def test_settings_shows_theme_and_discovers_installed_model(self):
        with patch("translator.ui.gui_settings.installed_models", return_value={"local-translator:latest"}):
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

    def test_startup_discovers_installed_models_and_clears_missing_models(self):
        for names in ({"local:latest"}, set()):
            with patch("translator.ui.gui_settings.installed_models", return_value=names):
                self.app._discover_startup_models()
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    self.app.update()
                    time.sleep(0.01)
            self.assertEqual(tuple(self.app.tr_model_box["values"]), tuple(sorted(names)))
            self.assertEqual(self.app.tr_model_var.get(), "local:latest" if names else "")

    def test_live_reader_updates_batches_and_keeps_reading_position(self):
        self.app.reading_chapters.clear()
        self.app._update_reading(("简体中文", 1, "第一章", ["原文"] * 100,
                                  [f"译文 {i}" for i in range(100)]))
        self.app._open_reader()
        reader = self.app.reader_window
        self.addCleanup(reader.destroy)
        reader.update()
        reader.text.yview("30.0")
        reader.update()
        before = reader.text.index("@0,0")
        self.app.log_queue.put(("reading", ("简体中文", 1, "第一章", ["原文"] * 101,
                                            [f"译文 {i}" for i in range(101)])))
        self.app._poll_queue()
        reader.update()
        self.assertEqual(reader.text.index("@0,0"), before)
        self.assertIn("译文 100", reader.text.get("1.0", "end"))
        reader.bilingual.set(True)
        reader.render()
        self.assertIn("原文", reader.text.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
