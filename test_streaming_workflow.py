from contextlib import ExitStack
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import main
from glossary_manager import load_project_glossary
from network_workflow import network_path
from project_storage import load_json, load_translation_state, atomic_json
from source_epub import WorkInfo, SourceChapter
from streaming_workflow import run_streaming_workflow


class StreamingTests(unittest.TestCase):
    url = "https://kakuyomu.jp/works/123"

    def setup_pipeline(self, stack, folder, translate, fetch=None, wait=None):
        self.work = WorkInfo(self.url, "小说", "作者", "简介", "", [
            {"url": self.url + f"/episodes/{i}", "title": f"第{i}章"} for i in range(1, 4)])
        self.context = MagicMock()
        def default_fetch(page, url, title):
            text = "レオン" + url.rsplit("/", 1)[-1]
            return SourceChapter(url, title, [text], [{"type": "text", "text": text}])
        stack.enter_context(patch("network_workflow.sync_playwright"))
        stack.enter_context(patch("main._launch_context", return_value=self.context))
        stack.enter_context(patch("main.select_active_page"))
        stack.enter_context(patch("network_workflow.open_work_page", return_value=False))
        stack.enter_context(patch("network_workflow.extract_work_info", return_value=self.work))
        self.fetch = stack.enter_context(patch("network_workflow.extract_source_chapter", side_effect=fetch or default_fetch))
        stack.enter_context(patch("network_workflow.download_chapter_images"))
        stack.enter_context(patch("network_workflow._wait", side_effect=wait))
        self.translate = stack.enter_context(patch("main.translate_episode", side_effect=translate))
        stack.enter_context(patch("main.ollama_chat_content", return_value=json.dumps({"entries": [{"source": "レオン", "target": "里昂"}]})))
        return {"output_dir": folder, "model": "test", "adaptive_translation_batches": False,
                "_capture_first_terms": True}

    def normal_translate(self, episode, cfg, work_dir):
        return [p.replace("レオン", "里昂") for p in episode.paragraphs]

    def test_translation_starts_before_second_fetch_and_acquisition_continues_during_translation(self):
        first_started, second_fetched = threading.Event(), threading.Event()
        seen = []
        def fetch(page, url, title):
            if url.endswith("/2"):
                self.assertTrue(first_started.is_set())
                second_fetched.set()
            text = "レオン" + url.rsplit("/", 1)[-1]
            return SourceChapter(url, title, [text], [{"type": "text", "text": text}])
        def wait(seconds, cfg):
            self.assertTrue(first_started.wait(5))
        def translate(episode, cfg, work_dir):
            if episode.url == "epub://chapter_0001":
                first_started.set()
                self.assertTrue(second_fetched.wait(5))
            if episode.paragraphs[0].startswith("レオン"):
                seen.append(dict(cfg["glossary"]))
            return self.normal_translate(episode, cfg, work_dir)
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, translate, fetch, wait)
            counts = []
            source, output, project = run_streaming_workflow(self.url, cfg, "zh-Hans", counts=counts.append)
            self.assertTrue(source.exists())
            self.assertTrue(output.exists())
            self.assertEqual(len(load_translation_state(project / "translation-project.json")["chapters"]), 3)
            self.assertEqual(seen[1]["レオン"], "里昂")
            self.assertEqual(counts[-1], {"acquired": 3, "translated": 3, "total": 3})
            self.assertEqual(load_json(network_path(self.url, cfg), {})["stage"], "complete")
            self.context.close.assert_called_once()
            from source_epub import extract_epub_chapters
            _, full_chapters = extract_epub_chapters(source, Path(folder) / "verify-assets")
            records = load_translation_state(project / "translation-project.json")["chapters"]
            self.assertEqual([ch.url for ch in full_chapters], [ch["url"] for ch in records])

    def test_translation_failure_keeps_checkpoints_and_resume_skips_saved_body(self):
        def translate(episode, cfg, work_dir):
            if episode.url == "epub://chapter_0002":
                raise RuntimeError("model failed")
            return self.normal_translate(episode, cfg, work_dir)
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, translate)
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                run_streaming_workflow(self.url, cfg, "zh-Hans")
            project_path = next(Path(folder).rglob("translation-project.json"))
            before = load_translation_state(project_path)["chapters"]
            self.assertEqual(len(before), 1)
            self.assertEqual(load_project_glossary(project_path.parent)[0].target, "里昂")
            self.translate.side_effect = self.normal_translate
            self.translate.reset_mock()
            source, output, _ = run_streaming_workflow(self.url, cfg, "zh-Hans")
            self.assertTrue(output.exists())
            self.assertEqual(load_translation_state(project_path)["chapters"][:1], before)
            self.assertEqual(self.fetch.call_count, 3)
            self.assertNotIn("epub://chapter_0001", [call.args[0].url for call in self.translate.call_args_list])
            # Appended directory changes must not invalidate completed prefixes.
            self.work.episodes.append({"url": self.url + "/episodes/4", "title": "第4章"})
            _, output, _ = run_streaming_workflow(self.url, cfg, "zh-Hans")
            self.assertTrue(output.exists())
            self.assertEqual(len(load_translation_state(project_path)["chapters"]), 4)

    def test_acquisition_error_is_reported_without_exporting_incomplete_book(self):
        def fetch(page, url, title):
            if url.endswith("/2"):
                raise RuntimeError("fetch failed")
            return SourceChapter(url, title, ["レオン"], [{"type": "text", "text": "レオン"}])
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, self.normal_translate, fetch)
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                run_streaming_workflow(self.url, cfg, "zh-Hans")
            project_path = next(Path(folder).rglob("translation-project.json"))
            self.assertEqual(len(load_translation_state(project_path)["chapters"]), 1)
            self.assertEqual(load_json(network_path(self.url, cfg), {})["stage"], "failed")
            self.context.close.assert_called_once()

    def test_cancel_waiting_for_first_chapter_joins_producer(self):
        cancelled = threading.Event()
        def fetch(page, url, title):
            cancelled.set()
            return SourceChapter(url, title, ["原文"], [{"type": "text", "text": "原文"}])
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, self.normal_translate, fetch)
            cfg["_translation_cancelled"] = cancelled.is_set
            with self.assertRaises(main.TranslationCancelled):
                run_streaming_workflow(self.url, cfg, "zh-Hans")
            self.assertEqual(load_json(network_path(self.url, cfg), {})["stage"], "stopped")
            self.context.close.assert_called_once()

    def test_changed_saved_source_stops_resume(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, self.normal_translate)
            run_streaming_workflow(self.url, cfg, "zh-Hans")
            cache_path = network_path(self.url, cfg).parent / "source-cache.json"
            cache = load_json(cache_path, {})
            cache[self.work.episodes[0]["url"]]["paragraphs"] = ["变更正文"]
            cache[self.work.episodes[0]["url"]]["blocks"] = [{"type": "text", "text": "变更正文"}]
            atomic_json(cache_path, cache)
            with self.assertRaisesRegex(ValueError, "原文"):
                run_streaming_workflow(self.url, cfg, "zh-Hans")

    def test_streaming_preserves_images_in_project_and_export(self):
        def images(context, chapter):
            chapter.blocks.append({"type": "image", "url": "https://example.test/pic.png"})
            chapter.images = [{"key": "https://example.test/pic.png", "name": "pic.png",
                               "media_type": "image/png", "data": b"image fixture"}]
        with TemporaryDirectory() as folder, ExitStack() as stack:
            cfg = self.setup_pipeline(stack, folder, self.normal_translate)
            stack.enter_context(patch("network_workflow.download_chapter_images", side_effect=images))
            _, output, project = run_streaming_workflow(self.url, cfg, "zh-Hans")
            record = load_translation_state(project / "translation-project.json")["chapters"][0]
            self.assertTrue(Path(record["images"][0]["local_path"]).exists())
            from zipfile import ZipFile
            with ZipFile(output) as archive:
                contents = [archive.read(name) for name in archive.namelist() if name.endswith(".png")]
            self.assertIn(b"image fixture", contents)
