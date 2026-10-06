import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import urlopen
from zipfile import ZipFile

from translator.reading.saved_reading import SavedReading
from translator.acquisition.source_epub import SourceChapter, translated_source_epub
from translator.ui.web_reader import ReadingServer
from translator.storage.project_storage import atomic_json


class SavedReadingTests(unittest.TestCase):
    def test_reopening_book_reuses_reader_without_rebuilding_index(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "translation-project.json"
            atomic_json(path, {"chapters": [{"title": "one", "paragraphs": ["text"]}]})
            server = ReadingServer()
            try:
                address = server.open_path(path)
                with patch("translator.reading.saved_reading.SavedReading", side_effect=AssertionError("rebuilt")):
                    self.assertEqual(server.open_path(path), address)
                atomic_json(path, {"chapters": [{"title": "one", "paragraphs": ["updated"]}]})
                self.assertEqual(json.load(urlopen(address + "chapter?id=1"))["translations"], ["updated"])
            finally:
                server.close()

    def test_open_reader_discovers_new_saved_chapters_and_updates_navigation_catalog(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            path = Path(directory) / "translation-project.json"
            atomic_json(path, {"schema_version": 2, "chapter_count": 1})
            atomic_json(path.parent / "chapters" / "000001.json", {
                "title": "第一卷", "source_paragraphs": ["原文"], "paragraphs": ["译文一"]})
            book = SavedReading(path)
            server = ReadingServer()
            try:
                server.open_saved(book)
                first = json.load(urlopen(server.url() + "chapter?id=1"))
                atomic_json(path.parent / "chapters" / "000002.json", {
                    "title": "续章", "source_paragraphs": ["原文二"], "paragraphs": ["译文二"]})
                atomic_json(path, {"schema_version": 2, "chapter_count": 2})
                catalog = json.load(urlopen(server.url() + "catalog"))
                self.assertEqual([chapter["id"] for chapter in catalog["chapters"]], ["1", "2"])
                self.assertEqual(catalog["chapters"][1]["volume"], "第一卷")
                self.assertGreater(catalog["chapters"][0]["revision"], first["revision"])
                second = json.load(urlopen(server.url() + "chapter?id=2"))
                self.assertEqual(second["translations"], ["译文二"])
                self.assertEqual(second["revision"], catalog["chapters"][1]["revision"])
                self.assertFalse(book.refresh())
                server.open_saved(SavedReading(path))
                reopened = json.load(urlopen(server.url() + "catalog"))
                self.assertGreater(reopened["chapters"][0]["revision"], second["revision"])
            finally:
                server.close()

    def test_refresh_invalidates_cached_body_even_when_only_chapter_file_changes(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            path = Path(directory) / "translation-project.json"
            chapter = path.parent / "chapters" / "000001.json"
            atomic_json(path, {"schema_version": 2, "chapter_count": 1})
            atomic_json(chapter, {"title": "旧标题", "paragraphs": ["旧译文"]})
            book = SavedReading(path)
            old = book.chapter("1")
            atomic_json(chapter, {"title": "新标题", "paragraphs": ["已更新的译文"]})
            updated = book.chapter("1")
            self.assertEqual(updated["title"], "新标题")
            self.assertEqual(updated["translations"], ["已更新的译文"])
            self.assertGreater(updated["revision"], old["revision"])

    def test_append_reuses_unchanged_chapter_indexes_and_preserves_reading_limit(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            path = Path(directory) / "translation-project.json"
            first = path.parent / "chapters" / "000001.json"
            second = path.parent / "chapters" / "000002.json"
            atomic_json(path, {"schema_version": 2, "chapter_count": 1})
            atomic_json(first, {"title": "一", "paragraphs": ["译文一"]})
            book = SavedReading(path, chapter_limit=1)
            atomic_json(second, {"title": "二", "paragraphs": ["译文二"]})
            atomic_json(path, {"schema_version": 2, "chapter_count": 2})
            reads = []
            original = Path.read_text
            def read(p, *args, **kwargs):
                reads.append(p)
                return original(p, *args, **kwargs)
            with patch.object(Path, "read_text", read):
                self.assertTrue(book.refresh())
                self.assertFalse(book.refresh())
            self.assertNotIn(first, reads)
            self.assertIn(second, reads)
            self.assertEqual(len(book.catalog), 1)
            self.assertIsNone(book.chapter("2"))

    def test_legacy_project_and_replaced_epub_refresh(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            atomic_json(path, {"chapters": [{"title": "一", "translation": ["译文一"]}]})
            book = SavedReading(path)
            atomic_json(path, {"chapters": [{"title": "一", "translation": ["译文一"]},
                                           {"title": "二", "translation": ["译文二"]}]})
            self.assertEqual(len(book.catalog_snapshot()["chapters"]), 2)
            self.assertEqual(book.chapter("2")["translations"], ["译文二"])
            epub = Path(directory) / "book.epub"
            chapters = [SourceChapter("one", "一", ["旧正文"], blocks=[{"type": "text", "text": "旧正文"}])]
            translated_source_epub({"title": "book"}, chapters, epub, "en")
            reader = SavedReading(epub)
            reader.chapter("1")
            chapters[0].paragraphs = ["更新后的正文"]
            chapters[0].blocks = [{"type": "text", "text": "更新后的正文"}]
            translated_source_epub({"title": "book"}, chapters, epub, "en")
            self.assertEqual(reader.chapter("1")["translations"], ["更新后的正文"])

    def test_epub_only_reads_requested_body_and_keeps_bounded_cache(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "novel.epub"
            translated_source_epub({"title": "小说", "author": "作者"},
                [SourceChapter(f"epub://{i}", f"第{i}章", [f"正文{i}"],
                               blocks=[{"type": "text", "text": f"正文{i}"}]) for i in range(12)],
                path, "简体中文")
            reads = []
            original = ZipFile.read
            def read(archive, name, *args, **kwargs):
                reads.append(name)
                return original(archive, name, *args, **kwargs)
            with patch.object(ZipFile, "read", read):
                book = SavedReading(path)
                self.assertFalse(any(e["file"] in reads for e in book.entries))
                self.assertEqual(book.chapter("1")["translations"], ["正文0"])
                count = len(reads)
                book.chapter("1")
                self.assertEqual(len(reads), count)
                for c in book.catalog:
                    book.chapter(c["id"])
                self.assertEqual(len(book.loaded), 8)

    def test_project_catalog_cache_invalidates_and_http_loads_on_demand(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            path = Path(directory) / "translation-project.json"
            path.write_text(json.dumps({"schema_version": 2, "chapter_count": 1}), encoding="utf-8")
            records = path.parent / "chapters"
            records.mkdir()
            record = records / "000001.json"
            record.write_text(json.dumps({"title": "第一卷", "paragraphs": ["译文"]}), encoding="utf-8")
            SavedReading(path)
            original = Path.read_text
            reads = []
            def read(p, *args, **kwargs):
                reads.append(p)
                return original(p, *args, **kwargs)
            with patch.object(Path, "read_text", read):
                book = SavedReading(path)
                self.assertNotIn(record, reads)
                server = ReadingServer()
                try:
                    server.open_saved(book)
                    catalog = json.load(urlopen(server.url() + "catalog"))
                    self.assertEqual(catalog["chapters"][0]["volume"], "第一卷")
                    self.assertFalse(book.loaded)
                    chapter = json.load(urlopen(server.url() + "chapter?id=1"))
                    self.assertEqual(chapter["translations"], ["译文"])
                    self.assertIn(record, reads)
                    server.reset("live")
                    self.assertIsNone(server.saved)
                finally:
                    server.close()
            record.write_text(json.dumps({"title": "第二卷 新标题", "paragraphs": ["新译文"]}), encoding="utf-8")
            refreshed = SavedReading(path)
            self.assertEqual(refreshed.catalog[0]["volume"], "第二卷")
            self.assertEqual(refreshed.chapter("1")["translations"], ["新译文"])
