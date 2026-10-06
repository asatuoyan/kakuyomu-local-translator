import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from translator.storage.project_storage import atomic_json, load_json
from translator.ui.web_app import Application, create_server


class WebAppTests(unittest.TestCase):
    def test_recent_network_snapshot_uses_real_title_and_deduplicates_url(self):
        from translator.acquisition.network_workflow import network_path
        url = "https://kakuyomu.jp/works/456"
        network = network_path(url, self.app.cfg)
        source = network.parent / "source_cached.epub"
        snapshot = network.parent / "stream" / source.name
        atomic_json(network, {"url": url, "source": str(source), "work": {"title": "Real novel"}})
        atomic_json(self.app.history_path, [{"source": str(snapshot), "title": "source_cached"},
                                          {"url": url, "title": "Old title"}])
        recent = self.app.recent_tasks()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["title"], "Real novel")
        self.assertEqual(recent[0]["url"], url)
        self.assertEqual(recent[0]["source"], "")
        self.assertEqual(len(load_json(self.app.history_path, [])), 2)

    def test_cached_continuation_does_not_acquire_or_release_another_books_browser(self):
        from translator.acquisition.network_workflow import network_path
        url = "https://kakuyomu.jp/works/456"
        network = network_path(url, self.app.cfg)
        source = network.parent / "source_cached.epub"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        snapshot = source.parent / "stream" / source.name
        atomic_json(network, {"url": url, "source": str(source), "total_chapters": 10,
                            "acquired_chapters": 10, "acquisition_complete": True})
        atomic_json(self.project / "translation-project.json", {"language": "en",
                    "source_path": str(snapshot), "chapters": [{}]})
        owner = object()
        self.app._browser_owner = owner
        self.app.acquisition_task.update(running=True, url="https://kakuyomu.jp/works/789")
        done = threading.Event()
        def translate(actual, cfg, language, progress):
            self.assertEqual(actual, source)
            self.assertIs(self.app._browser_owner, owner)
            done.set()
            return self.project / "translated.epub"
        with patch("translator.engine.ensure_model"), patch("translator.ui.web_app.update_config"), \
             patch("translator.engine.translation_work_dir", return_value=self.project), \
             patch("translator.engine.translate_epub_language", side_effect=translate), \
             patch("translator.translation.streaming_workflow.run_streaming_workflow") as stream:
            book = next(p for p in self.app.projects() if p["id"] == "book")
            self.assertEqual(book["resume"]["source"], str(source))
            self.assertEqual(book["resume"]["url"], "")
            for settings in ({"url": url}, {"source": str(snapshot)}):
                done.clear()
                self.app.start({**settings, "model": "test", "language": "en"})
                self.assertTrue(done.wait(5))
                for _ in range(100):
                    if not self.app.task["running"]:
                        break
                    threading.Event().wait(.01)
                self.assertFalse(self.app.task["running"])
            self.assertIs(self.app._browser_owner, owner)
            self.assertTrue(self.app.acquisition_task["running"])
            stream.assert_not_called()

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

    def test_stop_status_is_reported_separately_for_each_task(self):
        self.app.task["running"] = True
        self.app.acquisition_task["running"] = True
        self.post("stop-acquisition", {})
        self.assertTrue(self.app.acquisition_task["stopping"])
        self.assertTrue(self.app.acquisition_cancel.is_set())
        self.assertFalse(self.app.cancel.is_set())
        self.assertNotIn("stopping", self.app.task)
        self.post("stop", {})
        self.assertTrue(self.app.task["stopping"])
        self.assertTrue(self.app.cancel.is_set())

    def test_models_only_returns_installed_names_and_resolves_default_alias(self):
        self.app.cfg["model"] = "murasaki"
        with patch("translator.engine.installed_models", return_value={"murasaki:latest", "other:7b", ""}):
            with urlopen(self.server.url + "api/models") as response:
                result = json.load(response)
        self.assertEqual(result["models"], ["murasaki:latest", "other:7b"])
        self.assertEqual(result["selected"], "murasaki:latest")
        self.app.cfg["model"] = "missing"
        with patch("translator.engine.installed_models", return_value={"installed:latest"}):
            self.assertEqual(self.app.models()["selected"], "installed:latest")
        with patch("translator.engine.installed_models", return_value=set()):
            self.assertEqual(self.app.models()["selected"], "")

    def test_projects_show_term_counts_and_active_project_before_completion(self):
        self.app.write_terms({"project": "book", "entries": [{"source": "レオン", "target": "里昂"}]})
        self.assertEqual(self.app.projects()[0]["term_count"], 1)
        self.app.project_ready(self.project)
        self.assertEqual(self.app.status()["task"]["project"], "book")

    def test_project_completion_requires_full_current_export(self):
        source = Path(self.tmp.name) / "network-workflows" / "123" / "source_123.epub"
        source.parent.mkdir(parents=True)
        output = self.project / "translated.epub"
        output.write_bytes(b"epub")
        atomic_json(source.parent / "network.json", {
            "source": str(source), "url": "https://kakuyomu.jp/works/123", "total_chapters": 12})
        manifest = {"metadata": {"title": "book"}, "language": "en", "source_path": str(source),
                    "output_path": str(output), "chapters": [{}] * 10}
        atomic_json(self.project / "translation-project.json", manifest)
        project = self.app.projects()[0]
        self.assertEqual(project["total_chapters"], 12)
        self.assertFalse(project["completed"])
        manifest["chapters"] = [{}] * 12
        atomic_json(self.project / "translation-project.json", manifest)
        self.assertTrue(self.app.projects()[0]["completed"])
        manifest["epub_dirty"] = True
        atomic_json(self.project / "translation-project.json", manifest)
        self.assertFalse(self.app.projects()[0]["completed"])
        output.unlink()
        manifest["epub_dirty"] = False
        atomic_json(self.project / "translation-project.json", manifest)
        self.assertFalse(self.app.projects()[0]["completed"])

    def test_local_preview_is_unfinished_even_with_download(self):
        output = self.project / "preview.epub"
        output.write_bytes(b"epub")
        atomic_json(self.project / "translation-project.json", {
            "metadata": {"title": "preview"}, "language": "en", "output_path": str(output),
            "source_chapter_count": 20, "chapters": [{}] * 10})
        self.assertFalse(self.app.projects()[0]["completed"])

    def test_original_download_during_translation_and_source_resume_language(self):
        from translator.acquisition.network_workflow import network_path
        manifest = network_path("https://kakuyomu.jp/works/456", self.app.cfg)
        source = manifest.parent / f"source_{manifest.parent.name}.epub"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"completed original")
        atomic_json(manifest, {"url": "https://kakuyomu.jp/works/456", "source": str(source),
            "work": {"title": "Original"}, "stage": "acquired", "acquisition_complete": True,
            "acquired_chapters": 2, "total_chapters": 2})
        book = next(book for book in self.app.projects() if book.get("kind") == "source")
        self.app.task["running"] = True
        with urlopen(self.server.url + "api/download?file=" + book["source_download"].replace("\\", "/")) as response:
            self.assertEqual(response.read(), b"completed original")
            self.assertIn("original.epub", response.headers["Content-Disposition"])
        self.app.task["running"] = False
        with patch.object(self.app, "enqueue", return_value={"queued": True}) as enqueue:
            self.post("resume", {"project": book["id"], "model": "chosen", "language": "en"})
        self.assertEqual(enqueue.call_args.args, ("translation", {"source": str(source), "model": "chosen", "language": "en", "title": "Original"}))

    def test_restart_restores_project_download_resume_and_examples(self):
        source = Path(self.tmp.name) / "network-workflows" / "123" / "source_123.epub"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        output = self.project / "translated.epub"
        output.write_bytes(b"translated")
        atomic_json(source.parent / "network.json", {"source": str(source), "url": "https://kakuyomu.jp/works/123"})
        atomic_json(self.project / "translation-project.json", {"metadata": {"title": "book"}, "language": "en",
            "source_path": str(source), "output_path": str(output), "chapters": [
                {"title": "one", "source_paragraphs": ["レオンです"], "paragraphs": ["This is Leon"]}]})
        projects = self.app.projects()
        self.assertEqual(projects[0]["download"], str(output.relative_to(Path(self.tmp.name))))
        with patch.object(self.app, "start") as start:
            self.post("resume", {"project": "book", "model": "chosen"})
        self.assertEqual(start.call_args.args[0]["url"], "https://kakuyomu.jp/works/123")
        self.assertEqual(start.call_args.args[0]["model"], "chosen")
        self.assertEqual(self.app.term_examples(self.project, "レオン")["examples"][0]["translation"], "This is Leon")

    def test_terms_revision_and_stage_messages_are_independent(self):
        self.app.stage("translation", "translating")
        self.app.stage("acquisition", "fetching")
        self.app.stage("glossary", "extracting")
        self.app.model_activity("model generated")
        self.assertEqual(self.app.task["stages"], {"translation": "translating", "acquisition": "fetching", "glossary": "extracting · model generated"})
        self.app.write_terms({"project": "book", "entries": [{"source": "レオン", "target": "里昂"}]})
        revision = self.app.term_revision(self.project)
        with urlopen(self.server.url + "api/terms?project=book&revision=" + revision) as response:
            self.assertTrue(json.load(response)["unchanged"])
        self.app.write_terms({"project": "book", "entries": [{"source": "レオン", "target": "莱昂"}]})
        self.assertNotEqual(self.app.term_revision(self.project), revision)

    def test_model_activity_keeps_chapter_phase_and_generation_duration(self):
        self.app.stage("glossary", "章节 33/664 · 第33話 蟹 · 正在整理术语")
        self.app.model_activity(" · 已生成 1891 字 · 38 秒")
        self.app.model_activity(" · 已生成 1900 字 · 39 秒")
        message = self.app.task["stages"]["glossary"]
        self.assertIn("章节 33/664", message)
        self.assertIn("正在整理术语", message)
        self.assertIn("39 秒", message)
        self.assertNotIn("38 秒", message)

    def test_project_polling_reuses_discovery_and_json_but_observes_changes(self):
        original = Path.rglob
        with patch.object(Path, "rglob", autospec=True, side_effect=original) as scan, \
             patch("translator.ui.web_library.load_json", wraps=load_json) as read:
            self.app.projects()
            initial_reads = read.call_count
            self.app.projects()
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(read.call_count, initial_reads)
            atomic_json(self.project / "translation-project.json", {
                "metadata": {"title": "updated"}, "chapters": [{}]})
            self.assertEqual(self.app.projects()[0]["title"], "updated")
            self.assertEqual(read.call_count, initial_reads + 1)
            new = Path(self.tmp.name) / "new"
            atomic_json(new / "translation-project.json", {"metadata": {"title": "new"}, "chapters": []})
            self.app.project_ready(new)
            self.assertEqual(len(self.app.projects()), 2)
            self.assertEqual(scan.call_count, 2)

    def test_examples_read_saved_chapter_files_and_limit_results(self):
        atomic_json(self.project / "translation-project.json", {
            "metadata": {"title": "book"}, "schema_version": 2, "chapter_count": 1})
        atomic_json(self.project / "chapters" / "000001.json", {
            "title": "first", "source_paragraphs": ["レオン一", "レオン二", "レオン三"],
            "paragraphs": ["里昂一", "里昂二", "里昂三"]})
        from urllib.parse import urlencode
        with urlopen(self.server.url + "api/examples?" + urlencode({"project": "book", "source": "レオン"})) as response:
            result = json.load(response)
        self.assertEqual(result["examples"], [
            {"chapter": "first", "original": "レオン一", "translation": "里昂一"},
            {"chapter": "first", "original": "レオン二", "translation": "里昂二"}])
        self.assertEqual(self.app.term_examples(self.project, "アリス")["reason"], "term_not_found")

    def test_examples_distinguish_missing_originals(self):
        self.assertEqual(self.app.term_examples(self.project, "レオン"), {
            "examples": [], "reason": "no_originals"})

    def test_ollama_failure_and_missing_model_are_reported_without_starting(self):
        with patch("translator.engine.installed_models", side_effect=RuntimeError("Ollama unavailable")):
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.server.url + "api/models")
            self.assertEqual(raised.exception.code, 400)
        with patch("translator.engine.installed_models", return_value={"installed:latest"}), patch("translator.ui.web_app.update_config") as save:
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
        with patch("translator.ui.web_app.update_config"), patch("translator.engine.ensure_model"), \
             patch("translator.translation.streaming_workflow.run_streaming_workflow", side_effect=translate) as stream:
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
