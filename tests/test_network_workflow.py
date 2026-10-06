import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import translator.engine as main
from translator.acquisition.network_workflow import run_network_workflow, network_path
from translator.storage.project_storage import load_json, load_translation_state
from translator.acquisition.source_epub import SourceChapter, WorkInfo


class NetworkWorkflowTests(unittest.TestCase):
    url = "https://kakuyomu.jp/works/123"

    def test_web_login_shows_window_then_resumes_with_saved_profile_headlessly(self):
        from translator.acquisition.network_workflow import acquire_source
        contexts = [MagicMock() for _ in range(3)]
        pages = [MagicMock() for _ in range(3)]
        pages[1].url = "https://kakuyomu.jp/auth/login"
        pages[1].is_closed.return_value = False
        pages[1].wait_for_timeout.side_effect = lambda _: setattr(pages[1], "url", self.url)
        with TemporaryDirectory() as directory, \
                patch("translator.acquisition.network_workflow.sync_playwright"), \
                patch("translator.acquisition.browser_session.launch_context", side_effect=contexts) as launch, \
                patch("translator.acquisition.browser_session.select_active_page", side_effect=pages), \
                patch("translator.acquisition.network_workflow.open_work_page", side_effect=[True, True, False, False]), \
                patch("translator.acquisition.network_workflow.extract_work_info", side_effect=RuntimeError("reached acquisition")):
            progress = MagicMock()
            with self.assertRaisesRegex(RuntimeError, "reached acquisition"):
                acquire_source(self.url, {"output_dir": directory, "_automatic_browser_login": True,
                                         "headless": True, "browser_profile_dir": "same-profile"}, 1, progress=progress)
            self.assertEqual([call.args[1]["headless"] for call in launch.call_args_list], [True, False, True])
            self.assertTrue(all(call.args[1]["browser_profile_dir"] == "same-profile" for call in launch.call_args_list))
            for context in contexts:
                context.close.assert_called_once()
            self.assertIn("登录完成", progress.call_args.args[1])

    def test_web_with_existing_login_never_opens_a_visible_window(self):
        from translator.acquisition.network_workflow import acquire_source
        with TemporaryDirectory() as directory, \
                patch("translator.acquisition.network_workflow.sync_playwright"), \
                patch("translator.acquisition.browser_session.launch_context") as launch, \
                patch("translator.acquisition.browser_session.select_active_page"), \
                patch("translator.acquisition.network_workflow.open_work_page", return_value=False), \
                patch("translator.acquisition.network_workflow.extract_work_info", side_effect=RuntimeError("ready")):
            with self.assertRaisesRegex(RuntimeError, "ready"):
                acquire_source(self.url, {"output_dir": directory, "_automatic_browser_login": True}, 1)
            launch.assert_called_once()
            self.assertTrue(launch.call_args.args[1]["headless"])

    def test_closed_login_window_stops_and_releases_profile(self):
        from translator.acquisition.network_workflow import acquire_source
        contexts = [MagicMock(), MagicMock()]
        page = MagicMock()
        page.url = "https://kakuyomu.jp/auth/login"
        page.is_closed.return_value = True
        with TemporaryDirectory() as directory, \
                patch("translator.acquisition.network_workflow.sync_playwright"), \
                patch("translator.acquisition.browser_session.launch_context", side_effect=contexts), \
                patch("translator.acquisition.browser_session.select_active_page", return_value=page), \
                patch("translator.acquisition.network_workflow.open_work_page", return_value=True):
            with self.assertRaisesRegex(ValueError, "登录窗口已关闭"):
                acquire_source(self.url, {"output_dir": directory, "_automatic_browser_login": True}, 1)
            for context in contexts:
                context.close.assert_called_once()

    def test_cancelling_during_web_login_closes_visible_browser(self):
        from translator.acquisition.network_workflow import acquire_source
        contexts = [MagicMock(), MagicMock()]
        page = MagicMock()
        page.url = "https://kakuyomu.jp/auth/login"
        with TemporaryDirectory() as directory, \
                patch("translator.acquisition.network_workflow.sync_playwright"), \
                patch("translator.acquisition.browser_session.launch_context", side_effect=contexts), \
                patch("translator.acquisition.browser_session.select_active_page", return_value=page), \
                patch("translator.acquisition.network_workflow.open_work_page", return_value=True), \
                patch("translator.engine.check_translation_cancelled", side_effect=[None, None, main.TranslationCancelled()]):
            with self.assertRaises(main.TranslationCancelled):
                acquire_source(self.url, {"output_dir": directory, "_automatic_browser_login": True}, 1)
            for context in contexts:
                context.close.assert_called_once()

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
            with patch("translator.acquisition.network_workflow.sync_playwright"), patch("translator.acquisition.browser_session.launch_context", return_value=context), \
                    patch("translator.acquisition.browser_session.select_active_page"), patch("translator.acquisition.network_workflow.open_work_page", return_value=False), \
                    patch("translator.acquisition.network_workflow.extract_work_info", return_value=self.work(25)), \
                    patch("translator.acquisition.network_workflow.extract_source_chapter", side_effect=self.chapter) as fetch, \
                    patch("translator.acquisition.network_workflow.download_chapter_images"), patch("translator.acquisition.network_workflow._wait"), \
                    patch("translator.engine.translate_episode", side_effect=lambda ep, *_: ["译" + p for p in ep.paragraphs]) as translate:
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


    def test_incremental_check_fetches_only_new_chapters_and_no_change_keeps_epub(self):
        from translator.acquisition.network_workflow import acquire_source
        with TemporaryDirectory() as directory:
            cfg = {'output_dir': directory}
            with patch('translator.acquisition.network_workflow.sync_playwright'), \
                    patch('translator.acquisition.browser_session.launch_context', return_value=MagicMock()), \
                    patch('translator.acquisition.browser_session.select_active_page'), \
                    patch('translator.acquisition.network_workflow.open_work_page', return_value=False), \
                    patch('translator.acquisition.network_workflow.extract_work_info', side_effect=[self.work(2), self.work(3), self.work(3)]), \
                    patch('translator.acquisition.network_workflow.extract_source_chapter', side_effect=self.chapter) as fetch, \
                    patch('translator.acquisition.network_workflow.download_chapter_images'), \
                    patch('translator.acquisition.network_workflow._wait'):
                source, _ = acquire_source(self.url, cfg, 1, full=True)
                self.assertEqual(fetch.call_count, 2)
                acquire_source(self.url, cfg, 1, full=True, updates_only=True)
                self.assertEqual(fetch.call_count, 3)
                saved_bytes, saved_time = source.read_bytes(), source.stat().st_mtime_ns
                acquire_source(self.url, cfg, 1, full=True, updates_only=True)
                self.assertEqual(fetch.call_count, 3)
                self.assertEqual(source.read_bytes(), saved_bytes)
                self.assertEqual(source.stat().st_mtime_ns, saved_time)

    def test_incremental_check_missing_old_cache_does_not_refetch_or_overwrite_source(self):
        from translator.acquisition.network_workflow import acquire_source
        with TemporaryDirectory() as directory:
            cfg = {'output_dir': directory}
            with patch('translator.acquisition.network_workflow.sync_playwright'), \
                    patch('translator.acquisition.browser_session.launch_context', return_value=MagicMock()), \
                    patch('translator.acquisition.browser_session.select_active_page'), \
                    patch('translator.acquisition.network_workflow.open_work_page', return_value=False), \
                    patch('translator.acquisition.network_workflow.extract_work_info', side_effect=[self.work(2), self.work(3)]), \
                    patch('translator.acquisition.network_workflow.extract_source_chapter', side_effect=self.chapter) as fetch, \
                    patch('translator.acquisition.network_workflow.download_chapter_images'), \
                    patch('translator.acquisition.network_workflow._wait'):
                source, _ = acquire_source(self.url, cfg, 1, full=True)
                previous = source.read_bytes()
                cache_path = network_path(self.url, cfg).parent / 'source-cache.json'
                cache = load_json(cache_path, {})
                cache.pop(self.work(2).episodes[0]['url'])
                from translator.storage.project_storage import atomic_json
                atomic_json(cache_path, cache)
                fetch.reset_mock()
                with self.assertRaisesRegex(ValueError, '缓存缺失'):
                    acquire_source(self.url, cfg, 1, full=True, updates_only=True)
                fetch.assert_not_called()
                self.assertEqual(source.read_bytes(), previous)

    def test_full_cannot_acquire_before_preview_is_confirmed(self):
        with TemporaryDirectory() as directory, patch("translator.acquisition.network_workflow.acquire_source") as acquire:
            cfg = {"output_dir": directory, "model": "test"}
            with self.assertRaisesRegex(ValueError, "试译"):
                run_network_workflow(self.url, cfg, ["zh-Hans"], full=True)
            acquire.assert_not_called()

    def test_stop_keeps_downloaded_cache_and_closes_browser(self):
        with TemporaryDirectory() as directory:
            cfg = {"output_dir": directory, "model": "test"}
            context = MagicMock()
            with patch("translator.acquisition.network_workflow.sync_playwright"), patch("translator.acquisition.browser_session.launch_context", return_value=context), \
                    patch("translator.acquisition.browser_session.select_active_page"), patch("translator.acquisition.network_workflow.open_work_page", return_value=False), \
                    patch("translator.acquisition.network_workflow.extract_work_info", return_value=self.work(25)), \
                    patch("translator.acquisition.network_workflow.extract_source_chapter", side_effect=self.chapter), \
                    patch("translator.acquisition.network_workflow.download_chapter_images"), \
                    patch("translator.acquisition.network_workflow._wait", side_effect=main.TranslationCancelled()), \
                    patch("translator.acquisition.network_workflow.run_workflow") as translate:
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
            with patch("translator.acquisition.network_workflow.sync_playwright"), patch("translator.acquisition.browser_session.launch_context", return_value=MagicMock()), \
                    patch("translator.acquisition.browser_session.select_active_page"), patch("translator.acquisition.network_workflow.open_work_page", return_value=False), \
                    patch("translator.acquisition.network_workflow.extract_work_info", side_effect=work_for), \
                    patch("translator.acquisition.network_workflow.extract_source_chapter", side_effect=self.chapter), \
                    patch("translator.acquisition.network_workflow.download_chapter_images"), patch("translator.acquisition.network_workflow._wait"), \
                    patch("translator.engine.translate_episode", side_effect=lambda ep, *_: ["译" + p for p in ep.paragraphs]):
                first = run_network_workflow(self.url, cfg, ["zh-Hans"])
                second = run_network_workflow("https://kakuyomu.jp/works/456", cfg, ["zh-Hans"])
                self.assertNotEqual(first["previews"]["zh-Hans"]["project"], second["previews"]["zh-Hans"]["project"])
