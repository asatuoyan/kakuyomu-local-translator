import json
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError
from web_reader import ReadingServer
from unittest.mock import patch


class WebReaderTests(unittest.TestCase):
    @patch("web_reader.physical_lan_addresses", return_value={"192.168.1.20", "192.168.31.241"})
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
