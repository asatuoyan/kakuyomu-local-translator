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


class SavedReadingTests(unittest.TestCase):
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
