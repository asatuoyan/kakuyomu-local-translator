import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from translator.acquisition.network_workflow import network_path
from translator.engine import TranslationCancelled
from translator.storage.project_storage import atomic_json
from translator.ui.web_app import Application


class WebAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        cfg["output_dir"] = self.tmp.name
        self.app = Application(cfg)

    def saved_source(self, *, complete=True):
        manifest = network_path("https://kakuyomu.jp/works/123", self.app.cfg)
        source = manifest.parent / f"source_{manifest.parent.name}.epub"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"original EPUB")
        saved = {"url": "https://kakuyomu.jp/works/123", "source": str(source),
                 "work": {"title": "Source book"}, "total_chapters": 2, "acquired_chapters": 2,
                 "acquisition_complete": complete, "stage": "failed"}
        atomic_json(manifest, saved)
        self.app._discovery_until = 0
        return manifest, source, saved

    def test_fully_acquired_book_is_downloadable_before_translation_and_after_restart(self):
        _, source, _ = self.saved_source()
        restarted = Application(self.app.cfg)
        book = restarted.projects()[0]
        self.assertEqual(book["kind"], "source")
        self.assertEqual(book["source_download"], str(source.relative_to(self.root)))
        self.assertEqual(book["resume"]["source"], str(source))
        self.assertTrue(book["completed"])
        self.assertEqual(book["chapters"], 0)

    def test_original_download_merges_into_translation_without_duplicate_book(self):
        _, source, _ = self.saved_source()
        atomic_json(self.root / "translated" / "translation-project.json", {
            "metadata": {"title": "Translated book"}, "language": "en", "source_path": str(source),
            "chapters": [{"title": "one", "paragraphs": ["translated"]}]})
        books = self.app.projects()
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["source_download"], str(source.relative_to(self.root)))
        self.assertFalse(books[0]["completed"])
        self.assertEqual(books[0]["acquired_chapters"], 2)

    def test_partial_or_outside_source_is_not_offered_as_complete_download(self):
        manifest, source, saved = self.saved_source(complete=False)
        self.assertEqual(self.app.projects()[0]["source_download"], "")
        saved.update(acquisition_complete=True, source=str(self.root.parent / source.name))
        atomic_json(manifest, saved)
        self.assertEqual(self.app.projects()[0]["source_download"], "")
        saved.update(source=str(source), acquired_chapters=1)
        atomic_json(manifest, saved)
        self.assertEqual(self.app.projects()[0]["source_download"], "")

    def test_legacy_failed_translation_still_offers_verified_complete_original(self):
        from translator.acquisition.source_epub import SourceChapter, WorkInfo, build_source_epub
        manifest, source, saved = self.saved_source()
        saved.pop("acquisition_complete")
        atomic_json(manifest, saved)
        work = WorkInfo(saved["url"], "Book", "", "", "", [])
        chapters = [SourceChapter(f"episode/{i}", f"Chapter {i}", [f"Original {i}"]) for i in range(2)]
        build_source_epub(work, chapters, source)
        self.assertTrue(self.app.projects()[0]["source_download"])
        build_source_epub(work, chapters[:1], source)
        self.assertEqual(self.app.projects()[0]["source_download"], "")

    def test_independent_acquisition_can_be_cancelled_without_stopping_translation(self):
        entered = threading.Event()
        threads = []
        thread_class = threading.Thread
        def make_thread(*args, **kwargs):
            thread = thread_class(*args, **kwargs)
            threads.append(thread)
            return thread
        def acquire(url, cfg, *args, **kwargs):
            self.assertTrue(cfg["_automatic_browser_login"])
            entered.set()
            self.assertTrue(self.app.acquisition_cancel.wait(5))
            self.assertTrue(cfg["_translation_cancelled"]())
            raise TranslationCancelled()
        self.app.task = {"running": True, "url": "https://kakuyomu.jp/works/111", "message": "translating"}
        with patch("translator.acquisition.network_workflow.acquire_source", side_effect=acquire), \
                patch("translator.ui.web_app.threading.Thread", side_effect=make_thread):
            self.app.acquire({"url": "https://kakuyomu.jp/works/222"})
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.app.status()["acquisition_busy"])
                with self.assertRaisesRegex(ValueError, "正在获取"):
                    self.app.acquire({"url": "https://kakuyomu.jp/works/333"})
                self.app.acquisition_cancel.set()
            finally:
                self.app.acquisition_cancel.set()
                threads[0].join(5)
        self.assertFalse(threads[0].is_alive())
        self.assertTrue(self.app.task["running"])
        self.assertEqual(self.app.task["message"], "translating")
        self.assertFalse(self.app.cancel.is_set())
        self.assertFalse(self.app.acquisition_task["running"])
        self.assertIsNone(self.app._browser_owner)

    def test_previous_translation_cannot_release_a_new_acquisition_browser(self):
        old, new = object(), object()
        self.app._browser_owner = old
        self.app._release_browser(old)
        self.app._browser_owner = new
        self.app._release_browser(old)
        self.assertIs(self.app._browser_owner, new)
        self.app._release_browser(new)
        self.assertIsNone(self.app._browser_owner)

    def test_same_active_book_and_busy_browser_are_rejected(self):
        self.app.task = {"running": True, "url": "https://kakuyomu.jp/works/123"}
        with self.assertRaisesRegex(ValueError, "其他作品"):
            self.app.acquire({"url": "https://kakuyomu.jp/works/123"})
        self.app.task["running"] = False
        self.app._browser_owner = object()
        with patch("translator.engine.ensure_model") as model:
            with self.assertRaisesRegex(ValueError, "正在获取"):
                self.app.start({"url": "https://kakuyomu.jp/works/222"})
            model.assert_not_called()
