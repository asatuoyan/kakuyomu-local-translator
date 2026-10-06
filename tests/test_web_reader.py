import json
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError
from translator.ui.web_reader import ReadingServer, load_saved_reading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from translator.reading.saved_reading import SavedReading
from translator.storage.project_storage import atomic_json


class WebReaderTests(unittest.TestCase):
    def test_live_book_switch_keeps_previous_address_isolated(self):
        server = ReadingServer()
        self.addCleanup(server.close)
        server.reset("first")
        server.update(("en", 1, "one", [], ["first text"]))
        previous = server.url()
        server.reset("second")
        server.update(("en", 1, "two", [], ["second text"]))
        self.assertNotEqual(previous, server.url())
        self.assertEqual(json.load(urlopen(previous + "catalog"))["book"], "first")
        self.assertEqual(json.load(urlopen(previous + "chapter?id=en:1"))["translations"], ["first text"])
        self.assertEqual(json.load(urlopen(server.url() + "catalog"))["book"], "second")

    def test_saved_reader_redirect_pins_browser_and_respects_reading_limit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "translation-project.json"
            atomic_json(path, {"metadata": {"title": "Saved"}, "chapters": [
                {"title": "one", "paragraphs": ["one"]}, {"title": "two", "paragraphs": ["two"]}]})
            server = ReadingServer()
            self.addCleanup(server.close)
            server.open_saved(SavedReading(path))
            with urlopen(server.url()) as response:
                pinned = response.url
            self.assertNotEqual(pinned, server.url())
            limited = server.open_book(SavedReading(path, chapter_limit=1))
            self.assertNotEqual(pinned, limited)
            server.reset("another")
            self.assertEqual(len(json.load(urlopen(pinned + "catalog"))["chapters"]), 2)
            self.assertEqual(len(json.load(urlopen(limited + "catalog"))["chapters"]), 1)

    def test_saved_book_addresses_stay_isolated_and_keep_refreshing(self):
        with TemporaryDirectory() as directory:
            first, second = [Path(directory) / name / "translation-project.json" for name in ("one", "two")]
            for path, title in ((first, "Book one"), (second, "Book two")):
                atomic_json(path, {"metadata": {"title": title}, "chapters": [{"title": title, "paragraphs": [title]}]})
            server = ReadingServer()
            try:
                first_url = server.open_book(SavedReading(first))
                second_url = server.open_book(SavedReading(second))
                self.assertNotEqual(first_url, second_url)
                self.assertEqual(server.open_book(SavedReading(first)), first_url)
                server.reset("different live book")
                self.assertEqual(json.load(urlopen(first_url + "catalog"))["title"], "Book one")
                self.assertEqual(json.load(urlopen(second_url + "chapter?id=1"))["translations"], ["Book two"])
                atomic_json(first, {"metadata": {"title": "Book one"}, "chapters": [
                    {"title": "one", "paragraphs": ["one"]}, {"title": "new", "paragraphs": ["new"]}]})
                self.assertEqual(len(json.load(urlopen(first_url + "catalog"))["chapters"]), 2)
                self.assertEqual(len(json.load(urlopen(second_url + "catalog"))["chapters"]), 1)
            finally:
                server.close()
    def test_saved_project_reading_does_not_require_source_or_modify_files(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "translation-project.json"
            chapter = {"title": "第一卷", "paragraphs": ["保存的译文"]}
            path.write_text(json.dumps({"schema_version": 2, "chapter_count": 1,
                                       "metadata": {"title": "小说"}, "language": "中文"}), encoding="utf-8")
            (path.parent / "chapters").mkdir()
            record = path.parent / "chapters" / "000001.json"
            record.write_text(json.dumps(chapter), encoding="utf-8")
            before = (path.read_bytes(), record.read_bytes())
            identity, title, payloads = load_saved_reading(path)
            self.assertEqual(identity, str(path.resolve()))
            self.assertEqual(title, "小说")
            self.assertEqual(payloads, [("中文", 1, "第一卷", [""], ["保存的译文"])])
            self.assertEqual(before, (path.read_bytes(), record.read_bytes()))

    def test_saved_epub_reading(self):
        from translator.acquisition.source_epub import SourceChapter, translated_source_epub
        with TemporaryDirectory() as directory:
            path = Path(directory) / "finished.epub"
            translated_source_epub({"title": "已完成小说", "author": "作者"},
                                   [SourceChapter("epub://one", "第一章", ["译文正文"],
                                                  blocks=[{"type": "text", "text": "译文正文"}])],
                                   path, "简体中文")
            _, title, payloads = load_saved_reading(path)
            self.assertEqual(title, "已完成小说")
            self.assertEqual(payloads[0][2:], ("第一章", [""], ["译文正文"]))

    def test_catalog_groups_volumes_in_chapter_order_per_language(self):
        server = ReadingServer()
        self.addCleanup(server.close)
        for language, index, title in [("中文", 3, "后续"), ("中文", 2, "第二卷 开始"),
                                       ("中文", 1, "序章"), ("英文", 1, "Prologue")]:
            server.update((language, index, title, [], []))
        chapters = json.load(urlopen(server.url() + "catalog"))["chapters"]
        chinese = [c for c in chapters if c["language"] == "中文"]
        self.assertEqual([c["id"] for c in chinese], ["中文:1", "中文:2", "中文:3"])
        self.assertEqual([c["volume"] for c in chinese], ["未分卷", "第二卷", "第二卷"])
        self.assertEqual(next(c for c in chapters if c["language"] == "英文")["volume"], "未分卷")

    @patch("translator.ui.web_reader.physical_lan_addresses", return_value={"192.168.1.20", "192.168.31.241"})
    def test_lan_urls_use_physical_adapters_and_exclude_virtual_addresses(self, _physical):
        server = ReadingServer()
        self.addCleanup(server.close)
        self.assertEqual(server.lan_urls(), [server.url("192.168.1.20"), server.url("192.168.31.241")])

    def test_live_snapshots_reset_and_private_paths(self):
        server = ReadingServer()
        self.addCleanup(server.close)
        server.reset("book")
        self.assertIn("实时阅读", urlopen(server.url()).read().decode("utf-8"))
        with self.assertRaises(HTTPError) as error:
            urlopen(server.url().split(server.token)[0] + "catalog")
        self.assertEqual(error.exception.code, 404)
        server.update(("中文", 1, "第一章", ["原文"], ["译文"]))
        catalog = json.load(urlopen(server.url() + "catalog"))
        self.assertEqual(catalog["book"], "book")
        from urllib.parse import quote
        chapter = json.load(urlopen(server.url() + "chapter?id=" + quote(catalog["chapters"][0]["id"])))
        self.assertEqual(chapter["originals"], ["原文"])
        self.assertEqual(chapter["translations"], ["译文"])
        revision = chapter["revision"]
        server.update(("中文", 1, "第一章", ["原文", "二"], ["译文", "第二段"]))
        self.assertGreater(json.load(urlopen(server.url() + "catalog"))["chapters"][0]["revision"], revision)
        server.reset("next")
        self.assertEqual(json.load(urlopen(server.url() + "catalog"))["chapters"], [])
