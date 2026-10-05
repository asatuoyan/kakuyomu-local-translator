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

    def test_models_only_returns_installed_names_and_resolves_default_alias(self):
        self.app.cfg["model"] = "murasaki"
        with patch("main.installed_models", return_value={"murasaki:latest", "other:7b", ""}):
            with urlopen(self.server.url + "api/models") as response:
                result = json.load(response)
        self.assertEqual(result["models"], ["murasaki:latest", "other:7b"])
        self.assertEqual(result["selected"], "murasaki:latest")
        self.app.cfg["model"] = "missing"
        with patch("main.installed_models", return_value={"installed:latest"}):
            self.assertEqual(self.app.models()["selected"], "installed:latest")
        with patch("main.installed_models", return_value=set()):
            self.assertEqual(self.app.models()["selected"], "")

    def test_ollama_failure_and_missing_model_are_reported_without_starting(self):
        with patch("main.installed_models", side_effect=RuntimeError("Ollama unavailable")):
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.server.url + "api/models")
            self.assertEqual(raised.exception.code, 400)
        with patch("main.installed_models", return_value={"installed:latest"}), patch("web_app.update_config") as save:
            with self.assertRaises(HTTPError) as raised:
                self.post("start", {"url": "https://kakuyomu.jp/works/123", "model": "missing"})
            self.assertEqual(raised.exception.code, 400)
            self.assertFalse(self.app.task["running"])
            save.assert_not_called()

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

    def test_task_uses_streaming_acquisition_with_first_names(self):
        event = threading.Event()
        source = Path(self.tmp.name) / "source.epub"
        source.write_bytes(b"epub")
        output = self.project / "translated.epub"
        def translate(url, cfg, language, progress, counts):
            self.assertTrue(cfg["_capture_first_terms"])
            self.assertEqual(url, "https://kakuyomu.jp/works/123")
            counts({"acquired": 1, "translated": 1, "total": 1})
            progress(100, "完成")
            event.set()
            return source, output, self.project
        with patch("web_app.update_config"), patch("main.ensure_model"), \
             patch("streaming_workflow.run_streaming_workflow", side_effect=translate) as stream:
            self.app.start({"url": "https://kakuyomu.jp/works/123", "language": "zh-Hans", "model": "test"})
            self.assertTrue(event.wait(5))
            for _ in range(100):
                if not self.app.status()["task"]["running"]:
                    break
                threading.Event().wait(.01)
            self.assertFalse(self.app.task["running"])
            stream.assert_called_once()
            self.assertEqual(self.app.task["counts"]["translated"], 1)
            self.assertEqual(self.app.task["project"], "book")
