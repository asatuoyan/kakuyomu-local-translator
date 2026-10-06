import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

import translator.engine as main


class TranslationRetryTests(unittest.TestCase):
    def test_long_chapter_is_batched_and_saved_for_resume(self):
        paragraphs = [f"第{i}段：" + "原文" * 50 for i in range(2000)]
        episode = main.Episode(url="long", work_title="long", episode_title="long",
                               paragraphs=paragraphs, blocks=[])
        progress = Mock()
        cfg = {"model": "test", "target_language": "简体中文", "check_glossary": False,
               "translation_chunk_chars": 2200, "translation_chunk_paragraphs": 40,
               "_episode_progress": progress}
        with TemporaryDirectory() as temporary, patch("builtins.print"), patch(
                "translator.engine.translate_chunk", side_effect=lambda batch, *_: ["译" + p for p in batch]) as chunk:
            result = main.translate_episode(episode, cfg, Path(temporary))
            self.assertEqual(result, ["译" + p for p in paragraphs])
            self.assertGreater(chunk.call_count, 90)
            for call in chunk.call_args_list:
                self.assertLessEqual(sum(map(len, call.args[0])), 2200)
            progress.assert_called_with(2000, 2000)
            chunk.reset_mock()
            self.assertEqual(main.translate_episode(episode, cfg, Path(temporary)), result)
            chunk.assert_not_called()

    def setUp(self):
        self.cfg = {
            "model": "test", "review_model": "review", "target_language": "繁體中文",
            "translation_chunk_paragraphs": 1, "check_glossary": False,
            "translation_max_retries": 2, "translation_retry_delay_seconds": 0,
        }
        self.episode = main.Episode(
            url="epub://test", work_title="test", episode_title="test",
            paragraphs=["一", "二"], blocks=[],
        )

    def test_failed_batch_recovers_and_completed_batches_stay_cached(self):
        with TemporaryDirectory() as temporary, patch("translator.engine.translate_chunk") as chunk:
            chunk.side_effect = [["譯一"], requests.Timeout("timeout"), ["譯二"]]
            folder = Path(temporary)
            self.assertEqual(main.translate_episode(self.episode, self.cfg, folder), ["譯一", "譯二"])
            self.assertEqual([call.args[0] for call in chunk.call_args_list], [["一"], ["二"], ["二"]])
            chunk.reset_mock()
            self.assertEqual(main.translate_episode(self.episode, self.cfg, folder), ["譯一", "譯二"])
            chunk.assert_not_called()

    def test_progress_tracks_saved_batches_and_cached_paragraphs(self):
        progress = Mock()
        live = Mock()
        self.cfg["_episode_progress"] = progress
        self.cfg["_episode_live"] = live
        with TemporaryDirectory() as temporary, patch("translator.engine.translate_chunk") as chunk:
            folder = Path(temporary)
            chunk.side_effect = [["譯一"], ["譯二"]]
            main.translate_episode(self.episode, self.cfg, folder)
            self.assertEqual([call.args for call in progress.call_args_list],
                             [(0, 2), (1, 2), (2, 2)])
            self.assertEqual(live.call_args_list[1].args[0], ["譯一", ""])
            self.assertEqual(live.call_args_list[2].args[0], ["譯一", "譯二"])
            live.reset_mock()
            progress.reset_mock()
            chunk.reset_mock()
            main.translate_episode(self.episode, self.cfg, folder)
            progress.assert_called_once_with(2, 2)
            self.assertEqual(live.call_args_list[0].args[0], ["譯一", "譯二"])
            chunk.assert_not_called()


    def test_exhaustion_keeps_successful_batch_for_resume(self):
        with TemporaryDirectory() as temporary, patch("translator.engine.translate_chunk") as chunk:
            folder = Path(temporary)
            chunk.side_effect = [["譯一"]] + [requests.ConnectionError("offline")] * 3
            with self.assertRaises(requests.ConnectionError):
                main.translate_episode(self.episode, self.cfg, folder)
            self.assertEqual(chunk.call_count, 4)
            chunk.reset_mock()
            chunk.side_effect = [["譯二"]]
            self.assertEqual(main.translate_episode(self.episode, self.cfg, folder), ["譯一", "譯二"])
            self.assertEqual(chunk.call_args.args[0], ["二"])

    def test_permanent_errors_are_not_retried(self):
        response = requests.Response()
        response.status_code = 404
        for error in (RuntimeError("model not found"),
                      requests.HTTPError(response=response), ValueError("bad config")):
            with self.subTest(error=error):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    main.run_translation_batch(operation, self.cfg, "test")
                operation.assert_called_once()

    def test_backoff_and_cancellation(self):
        self.cfg["translation_retry_delay_seconds"] = 10
        stopped = False

        def stop(_seconds):
            nonlocal stopped
            stopped = True

        self.cfg["_translation_cancelled"] = lambda: stopped
        operation = Mock(side_effect=requests.Timeout("timeout"))
        with patch("translator.engine.time.sleep", side_effect=stop) as sleep:
            with self.assertRaises(main.TranslationCancelled):
                main.run_translation_batch(operation, self.cfg, "test")
        operation.assert_called_once()
        sleep.assert_called_once_with(0.2)

    def test_backoff_increases_and_is_capped(self):
        self.cfg.update(translation_retry_delay_seconds=40, translation_max_retries=3)
        operation = Mock(side_effect=[requests.Timeout()] * 3 + ["ok"])
        with patch("translator.engine.time.sleep") as sleep:
            self.assertEqual(main.run_translation_batch(operation, self.cfg, "test"), "ok")
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 160)


if __name__ == "__main__":
    unittest.main()
