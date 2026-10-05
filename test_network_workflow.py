import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import main
from network_workflow import run_network_workflow, network_path
from project_storage import load_json, load_translation_state
from source_epub import SourceChapter, WorkInfo


class NetworkWorkflowTests(unittest.TestCase):
    url = "https://kakuyomu.jp/works/123"

    def work(self, count):
        return WorkInfo(self.url, "测试小说", "作者", "", "", [
            {"url": self.url + f"/episodes/{i}", "title": f"第{i}章"} for i in range(1, count + 1)])

    def chapter(self, _page, url, title):
        text = "正文" + url.rsplit("/", 1)[-1]
        return SourceChapter(url, title, [text], blocks=[{"type": "text", "text": text}])

    def test_url_preview_downloads_only_twenty_then_full_reuses_source_and_translation(self):
        with TemporaryDirectory() as directory:
            cfg = {"output_dir": directory, "model": "test", "check_glossary": False,
                   "adaptive_translation_batches": False}
            context = MagicMock()
            with patch("network_workflow.sync_playwright"), patch("main._launch_context", return_value=context), \
                    patch("main.select_active_page"), patch("network_workflow.open_work_page", return_value=False), \
                    patch("network_workflow.extract_work_info", return_value=self.work(25)), \
                    patch("network_workflow.extract_source_chapter", side_effect=self.chapter) as fetch, \
                    patch("network_workflow.download_chapter_images"), patch("network_workflow._wait"), \
                    patch("main.translate_episode", side_effect=lambda ep, *_: ["译" + p for p in ep.paragraphs]) as translate:
                state = run_network_workflow(self.url, cfg, ["zh-Hans"])
                self.assertEqual(fetch.call_count, 20)
                self.assertEqual(state["stage"], "awaiting_confirmation")
                self.assertEqual(state["network_total_chapters"], 25)
                self.assertEqual(state["network_url"], self.url)
                preview = state["previews"]["zh-Hans"]
                project_path = Path(preview["project"]) / "translation-project.json"
                before = load_translation_state(project_path)["chapters"]
                self.assertEqual(len(before), 20)
                translate.reset_mock()
                complete = run_network_workflow(self.url, cfg, ["zh-Hans"], full=True)
                self.assertEqual(fetch.call_count, 25)
                self.assertEqual(complete["stage"], "complete")
                records = load_translation_state(project_path)["chapters"]
                self.assertEqual(records[:20], before)
                self.assertEqual(len(records), 25)
                body_calls = [call for call in translate.call_args_list if call.args[0].url.startswith("epub://chapter_")
                              and not call.args[0].url.endswith("#title")]
                self.assertEqual(len(body_calls), 5)
                self.assertEqual(context.close.call_count, 2)
                again = run_network_workflow(self.url, cfg, ["zh-Hans"], count=3)
                self.assertEqual(again["previews"]["zh-Hans"]["chapters"], 3)
                self.assertEqual(fetch.call_count, 25)
                self.assertEqual(len(load_translation_state(project_path)["chapters"]), 25)

    def test_full_cannot_acquire_before_preview_is_confirmed(self):
        with TemporaryDirectory() as directory, patch("network_workflow.acquire_source") as acquire:
            cfg = {"output_dir": directory, "model": "test"}
            with self.assertRaisesRegex(ValueError, "试译"):
                run_network_workflow(self.url, cfg, ["zh-Hans"], full=True)
            acquire.assert_not_called()

    def test_stop_keeps_downloaded_cache_and_closes_browser(self):
        with TemporaryDirectory() as directory:
            cfg = {"output_dir": directory, "model": "test"}
            context = MagicMock()
            with patch("network_workflow.sync_playwright"), patch("main._launch_context", return_value=context), \
                    patch("main.select_active_page"), patch("network_workflow.open_work_page", return_value=False), \
                    patch("network_workflow.extract_work_info", return_value=self.work(25)), \
                    patch("network_workflow.extract_source_chapter", side_effect=self.chapter), \
                    patch("network_workflow.download_chapter_images"), \
                    patch("network_workflow._wait", side_effect=main.TranslationCancelled()), \
                    patch("network_workflow.run_workflow") as translate:
                with self.assertRaises(main.TranslationCancelled):
                    run_network_workflow(self.url, cfg, ["zh-Hans"])
                translate.assert_not_called()
                context.close.assert_called_once()
                self.assertEqual(len(load_json(network_path(self.url, cfg).parent / "source-cache.json", {})), 1)
                self.assertEqual(load_json(network_path(self.url, cfg), {})["stage"], "stopped")

    def test_two_online_books_have_separate_translation_projects(self):
        with TemporaryDirectory() as directory:
            cfg = {"output_dir": directory, "model": "test", "adaptive_translation_batches": False}
            def work_for(_page, url):
                return WorkInfo(url, "同名小说", "", "", "", [{"url": url + "/episodes/1", "title": "第一章"}])
            with patch("network_workflow.sync_playwright"), patch("main._launch_context", return_value=MagicMock()), \
                    patch("main.select_active_page"), patch("network_workflow.open_work_page", return_value=False), \
                    patch("network_workflow.extract_work_info", side_effect=work_for), \
                    patch("network_workflow.extract_source_chapter", side_effect=self.chapter), \
                    patch("network_workflow.download_chapter_images"), patch("network_workflow._wait"), \
                    patch("main.translate_episode", side_effect=lambda ep, *_: ["译" + p for p in ep.paragraphs]):
                first = run_network_workflow(self.url, cfg, ["zh-Hans"])
                second = run_network_workflow("https://kakuyomu.jp/works/456", cfg, ["zh-Hans"])
                self.assertNotEqual(first["previews"]["zh-Hans"]["project"], second["previews"]["zh-Hans"]["project"])
