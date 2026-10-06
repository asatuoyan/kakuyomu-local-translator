import json
import threading
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ebooklib import epub

import translator.engine as main
from translator.storage.project_storage import load_translation_state
from translator.acquisition.source_epub import extract_epub_chapters
from tests.test_main import FakeResponse


class StreamIntegrityTests(unittest.TestCase):
    def test_truncated_stream_is_rejected_and_not_cached(self):
        cfg = {"ollama_url": "http://localhost", "model": "test", "target_language": "en",
               "translation_max_retries": 0, "check_glossary": False}
        response = FakeResponse([b'{"message":{"content":"unfinished"}}'])
        episode = main.Episode("one", "", "", ["original"])
        with TemporaryDirectory() as folder, patch("translator.engine.requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "完成标记"):
                main.translate_episode(episode, cfg, Path(folder))
            self.assertTrue(response.closed)
            with patch("translator.engine.translate_chunk", return_value=["complete"]) as translate:
                self.assertEqual(main.translate_episode(episode, cfg, Path(folder)), ["complete"])
                translate.assert_called_once()

    def test_length_limited_done_is_rejected(self):
        response = FakeResponse([b'{"message":{"content":"partial"},"done":true,"done_reason":"length"}'])
        with patch("translator.engine.requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "长度限制"):
                main.ollama_chat_content({}, {"ollama_url": "http://localhost"})
        self.assertTrue(response.closed)

    def test_cancel_does_not_wait_for_blocked_network_or_accept_late_result(self):
        entered, release, stopped, closed = (threading.Event() for _ in range(4))

        class BlockedResponse(FakeResponse):
            def iter_lines(self):
                entered.set()
                release.wait(3)
                yield b'{"message":{"content":"late"},"done":true}'

            def close(self):
                closed.set()

        cfg = {"ollama_url": "http://localhost", "_translation_cancelled": stopped.is_set}

        def stop_when_connected():
            entered.wait(2)
            stopped.set()

        stopper = threading.Thread(target=stop_when_connected)
        stopper.start()
        try:
            with patch("translator.engine.requests.post", return_value=BlockedResponse([])):
                start = time.monotonic()
                with self.assertRaises(main.TranslationCancelled):
                    main.ollama_chat_content({}, cfg)
                self.assertLess(time.monotonic() - start, 1.5)
                release.set()
                self.assertTrue(closed.wait(2))
        finally:
            release.set()
            stopper.join(2)

    def test_recursive_split_checks_cancellation(self):
        stopped = False

        def reply(*_):
            nonlocal stopped
            stopped = True
            return "one paragraph"

        cfg = {"model": "test", "target_language": "en", "_translation_cancelled": lambda: stopped}
        with patch("translator.engine.ollama_chat_content", side_effect=reply) as chat:
            with self.assertRaises(main.TranslationCancelled):
                main.translate_chunk(["a", "b"], cfg)
            chat.assert_called_once()


class ContextCacheTests(unittest.TestCase):
    def test_identical_dialogue_in_other_chapter_is_not_reused(self):
        cfg = {"model": "test", "target_language": "en", "check_glossary": False}
        with TemporaryDirectory() as folder, patch("translator.engine.translate_chunk", return_value=["yes"]) as translate:
            for url in ("chapter-one", "chapter-two", "chapter-one"):
                main.translate_episode(main.Episode(url, "", "", ["はい"]), cfg, Path(folder))
            self.assertEqual(translate.call_count, 2)

    def test_changed_surrounding_text_invalidates_dialogue(self):
        cfg = {"model": "test", "target_language": "en", "check_glossary": False}
        with TemporaryDirectory() as folder, patch("translator.engine.translate_chunk", side_effect=lambda text, *_: text) as translate:
            main.translate_episode(main.Episode("one", "", "", ["彼女", "はい"]), cfg, Path(folder))
            main.translate_episode(main.Episode("one", "", "", ["彼", "はい"]), cfg, Path(folder))
            self.assertEqual(translate.call_count, 2)
            self.assertEqual(translate.call_args.args[0], ["彼", "はい"])


class ProjectCompatibilityTests(unittest.TestCase):
    def test_added_chapter_can_resume_but_changed_chapter_is_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            book = self.make_book(root)
            first = main.SourceChapter("one", "Title", ["竜がいる", "unchanged"])
            second = main.SourceChapter("two", "Second", ["新しい章"])
            source = root / "source.epub"
            source.write_bytes(b"source with another chapter")
            resumed = main.TranslationBook(source, root, {"title": "Book"}, "en", [first, second])
            self.assertTrue(resumed.completed(1, first))
            self.assertFalse(resumed.completed(2, second))
            self.assertTrue(resumed.state["epub_dirty"])
            source.write_bytes(b"changed previous chapter")
            changed = main.SourceChapter("one", "Title", ["竜はいない", "unchanged"])
            with self.assertRaisesRegex(ValueError, "原文已變更"):
                main.TranslationBook(source, root, {"title": "Book"}, "en", [changed, second])
            self.assertEqual(load_translation_state(book.path)["source_hash"], resumed.state["source_hash"])

    def make_book(self, root):
        source = root / "source.epub"
        source.write_bytes(b"source")
        book = main.TranslationBook(source, root, {"title": "Book"}, "en")
        original = main.SourceChapter("one", "Title", ["竜がいる", "unchanged"],
                                      [{"type": "text", "text": "竜がいる"}, {"type": "text", "text": "unchanged"}])
        translated = main._translated_chapter(original, ["wrong", "keep"])
        book.save(1, original, translated)
        return book

    def test_new_project_audit_and_partial_retranslation_rebuild_all_outputs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            book = self.make_book(root)
            cfg = {"model": "test", "glossary_by_language": {"en": {"竜": "dragon"}}}
            project = main.load_review_project(root)
            entries = main.project_glossary(cfg, root, project)
            self.assertEqual(len(main.audit_project(project, entries)), 1)
            with patch("translator.engine.translate_chunk", return_value=["dragon here"]) as translate:
                output = main.retranslate_project(root, cfg, ["竜"])
                self.assertEqual(translate.call_args.args[0], ["竜がいる"])
                translate.assert_called_once()
            restored = main.load_review_project(root)
            self.assertEqual(restored["chapters"][0]["paragraphs"], ["dragon here", "keep"])
            self.assertEqual(main.audit_project(restored, entries), [])
            self.assertTrue(output.exists())
            part = root / "Book_en_0001-0001.epub"
            self.assertTrue(part.exists())
            for path in (output, part):
                with zipfile.ZipFile(path) as archive:
                    text = "".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml"))
                    self.assertIn("dragon here", text)
                    self.assertIn("keep", text)
            manifest = json.loads(book.path.read_text(encoding="utf-8"))
            self.assertNotIn("chapters", manifest)
            self.assertEqual(manifest["chapter_count"], 1)

    def test_selected_paragraph_retranslation_changes_only_that_paragraph(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_book(root)
            with patch("translator.engine.translate_chunk", return_value=["updated"]) as translate:
                main.retranslate_project(root, {"model": "test"}, [],
                                         paragraph_location=("Title", 2, "unchanged"))
            translate.assert_called_once()
            self.assertEqual(translate.call_args.args[0], ["unchanged"])
            self.assertEqual(main.load_review_project(root)["chapters"][0]["paragraphs"],
                             ["wrong", "updated"])

    def test_legacy_translation_project_recovers_original_and_preserves_backup(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.epub"
            source.write_bytes(b"source")
            original = main.SourceChapter("one", "Title", ["original"], [])
            state = {"source_hash": main.hashlib.sha256(b"source").hexdigest(), "language": "en",
                     "metadata": {"title": "Book"}, "chapters": [{"url": "one", "title": "Title",
                     "source_hash": main.compute_content_hash(original.paragraphs), "paragraphs": ["translated"],
                     "blocks": [], "images": []}]}
            path = root / "translation-project.json"
            main.atomic_json(path, state)
            with self.assertRaises(main.MissingProjectSource):
                main.load_review_project(root)
            with patch("translator.engine.extract_epub_chapters", return_value=({}, [original])):
                restored = main.load_review_project(root, source)
            self.assertEqual(restored["chapters"][0]["source_paragraphs"], ["original"])
            self.assertEqual(json.loads(path.with_suffix(".legacy.json").read_text(encoding="utf-8")), state)
            self.assertEqual(load_translation_state(path)["chapters"][0]["paragraphs"], ["translated"])

    def test_legacy_project_and_unaligned_project(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"chapters": [{"title": "old", "japanese": ["original"], "translation": ["translated"]}]}
            main.atomic_json(root / "project.json", state)
            self.assertEqual(main.load_review_project(root), state)
            self.assertEqual(main.audit_project(state, []), [])
            state["chapters"][0]["translation"] = []
            with self.assertRaisesRegex(ValueError, "对应关系"):
                main.audit_project(state, [])

    def test_saving_next_chapter_does_not_rewrite_previous_record(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            book = self.make_book(root)
            record = root / "chapters" / "000001.json"
            before = record.stat().st_mtime_ns
            original = main.SourceChapter("two", "two", ["second"], [])
            book.save(2, original, original)
            self.assertEqual(record.stat().st_mtime_ns, before)
            self.assertEqual(len(load_translation_state(book.path)["chapters"]), 2)

    def test_append_project_preserves_unaffected_records_and_base(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"base_epub": "base.epub", "work_title": "Book", "language": "en", "chapters": [
                {"url": "one", "title": "one", "japanese": ["竜", "stay"], "translation": ["wrong", "keep"],
                 "blocks": [{"type": "text", "translation": "wrong"}, {"type": "text", "translation": "keep"}], "images": []},
                {"url": "two", "title": "two", "japanese": ["other"], "translation": ["other"], "blocks": [], "images": []}]}
            main.atomic_json(root / "project.json", state)
            with patch("translator.engine.translate_chunk", return_value=["dragon"]), \
                 patch("translator.engine.build_extended_epub", return_value=root / "output.epub") as build:
                main.retranslate_project(root, {"model": "test"}, ["竜"])
            restored = main.load_review_project(root)
            self.assertEqual(restored["chapters"][1], state["chapters"][1])
            self.assertEqual(restored["chapters"][0]["blocks"][0]["translation"], "dragon")
            self.assertEqual(restored["base_epub"], "base.epub")
            build.assert_called_once()

    def test_text_import_project_without_base_can_be_retranslated(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"base_epub": None, "work_title": "Book", "language": "en", "chapters": [
                {"url": "one", "title": "one", "japanese": ["竜", "stay"], "translation": ["wrong", "keep"],
                 "blocks": [{"type": "text", "text": "wrong"}, {"type": "text", "text": "keep"}], "images": []}]}
            main.atomic_json(root / "project.json", state)
            with patch("translator.engine.translate_chunk", return_value=["dragon"]):
                output = main.retranslate_project(root, {"model": "test"}, ["竜"])
            self.assertTrue(output.exists())
            self.assertEqual(main.load_review_project(root)["chapters"][0]["translation"], ["dragon", "keep"])


class EpubExtractionTests(unittest.TestCase):
    def make_epub(self, root, body, images=None):
        book = epub.EpubBook()
        book.set_identifier("test")
        book.set_title("Test")
        book.set_language("ja")
        item = epub.EpubHtml(uid="chapter", file_name="Text/chapter.xhtml", title="Title")
        item.content = body
        book.add_item(item)
        for name, content in (images or {}).items():
            book.add_item(epub.EpubItem(uid=name, file_name=name, media_type="image/png", content=content))
        book.spine = [item]
        book.toc = [item]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        path = root / "book.epub"
        epub.write_epub(str(path), book)
        return path

    def test_mixed_content_other_blocks_ruby_and_duplicate_image_names(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.make_epub(root,
                '<h1>Title</h1><div>outside<p>before<img src="../A/pic.png"/>after</p>'
                '<blockquote>quote</blockquote><ul><li>list</li></ul><h2>section</h2>'
                '<p><ruby>漢字<rt>かんじ</rt></ruby></p><p>line<br/>break</p>'
                '<img src="../B/pic.png#image"/></div>',
                {"A/pic.png": b"first", "B/pic.png": b"second"})
            _, chapters = extract_epub_chapters(path, root / "assets")
            chapter = chapters[0]
            self.assertEqual(chapter.paragraphs, ["outside", "before", "after", "quote", "list", "section", "漢字", "line", "break"])
            self.assertEqual([Path(image["local_path"]).read_bytes() for image in chapter.images], [b"first", b"second"])
            self.assertEqual(len({image["name"] for image in chapter.images}), 2)
            output = root / "translated.epub"
            main.translated_source_epub({"title": "Test"}, chapters, output, "en")
            with zipfile.ZipFile(output) as archive:
                data = [archive.read(name) for name in archive.namelist() if name.endswith(".png")]
                self.assertCountEqual(data, [b"first", b"second"])

    def test_image_only_chapter_is_preserved(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.make_epub(root, '<h1>Title</h1><p><img src="../A/pic.png"/></p>', {"A/pic.png": b"image"})
            _, chapters = extract_epub_chapters(path, root / "assets")
            self.assertEqual(chapters[0].paragraphs, [])
            self.assertEqual(len(chapters[0].images), 1)

    def test_missing_image_reports_error_instead_of_silently_dropping_it(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.make_epub(root, '<p>text<img src="../missing.png"/></p>')
            with self.assertRaisesRegex(ValueError, "图片资源缺失"):
                extract_epub_chapters(path, root / "assets")


if __name__ == "__main__":
    unittest.main()
