import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from project_storage import atomic_json
from web_app import Application, create_server


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        cfg["output_dir"] = self.tmp.name
        self.app = Application(cfg)
        self.project = Path(self.tmp.name) / "book"
        atomic_json(self.project / "translation-project.json", {"metadata": {"title": "测试小说"},
                    "language": "簡體中文", "chapters": []})
        self.server = create_server(self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = self.server.url.split("/")[-2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, route, body, token=None):
        req = Request(self.server.url + "api/" + route, json.dumps(body).encode(),
                      {"Content-Type": "application/json", "X-Local-App": token or self.token})
        with urlopen(req) as response:
            return json.load(response)

    def test_terms_round_trip_merge_and_busy_protection(self):
        self.post("terms", {"project": "book", "entries": [{"source": "アリス", "target": "爱丽丝"}]})
        self.post("terms", {"project": "book", "merge": True,
                  "entries": [{"source": "アリス", "target": "艾莉丝"}, {"source": "レオン", "target": "里昂"}]})
        with urlopen(self.server.url + "api/export?project=book") as response:
            terms = json.load(response)["entries"]
            self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual({e["source"]: e["target"] for e in terms}, {"アリス": "艾莉丝", "レオン": "里昂"})
        self.app.task["running"] = True
        with self.assertRaises(HTTPError) as raised:
            self.post("terms", {"project": "book", "entries": []})
        self.assertEqual(raised.exception.code, 400)

    def test_local_token_and_path_boundary(self):
        with self.assertRaises(HTTPError) as raised:
            self.post("stop", {}, token="wrong")
        self.assertEqual(raised.exception.code, 403)
        with self.assertRaises(ValueError):
            self.app.project("../outside")

    def test_read_saved_project_and_download(self):
        atomic_json(self.project / "translation-project.json", {"metadata": {"title": "测试小说"},
                    "language": "簡體中文", "chapters": [{"title": "第一章", "paragraphs": ["里昂"]}]})
        result = self.post("read", {"project": "book"})
        try:
            with urlopen(result["url"] + "catalog") as response:
                self.assertEqual(len(json.load(response)["chapters"]), 1)
            (self.project / "book.epub").write_bytes(b"epub")
            with urlopen(self.server.url + "api/download?file=book/book.epub") as response:
                self.assertEqual(response.read(), b"epub")
        finally:
            self.app.reader.close()

    def test_task_acquires_full_book_then_translates_with_first_names(self):
        event = threading.Event()
        source = Path(self.tmp.name) / "source.epub"
        source.write_bytes(b"epub")
        output = self.project / "translated.epub"
        def translate(actual, cfg, language, progress):
            self.assertTrue(cfg["_capture_first_terms"])
            self.assertEqual(actual, source)
            progress(100, "完成")
            event.set()
            return output
        with patch("web_app.update_config"), patch("main.ensure_model"), \
             patch("network_workflow.acquire_source", return_value=(source, None)) as acquire, \
             patch("main.translation_work_dir", return_value=self.project), \
             patch("main.translate_epub_language", side_effect=translate):
            self.app.start({"url": "https://kakuyomu.jp/works/123", "language": "zh-Hans", "model": "test"})
            self.assertTrue(event.wait(5))
            for _ in range(100):
                if not self.app.status()["task"]["running"]:
                    break
                threading.Event().wait(.01)
            self.assertFalse(self.app.task["running"])
            self.assertTrue(acquire.call_args.kwargs["full"])
            self.assertEqual(self.app.task["project"], "book")
