import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

import main


class TranslationRetryTests(unittest.TestCase):
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
        with TemporaryDirectory() as temporary, patch("main.translate_chunk") as chunk:
            chunk.side_effect = [["譯一"], requests.Timeout("timeout"), ["譯二"]]
            folder = Path(temporary)
            self.assertEqual(main.translate_episode(self.episode, self.cfg, folder), ["譯一", "譯二"])
            self.assertEqual([call.args[0] for call in chunk.call_args_list], [["一"], ["二"], ["二"]])
            chunk.reset_mock()
            self.assertEqual(main.translate_episode(self.episode, self.cfg, folder), ["譯一", "譯二"])
            chunk.assert_not_called()


    def test_exhaustion_keeps_successful_batch_for_resume(self):
        with TemporaryDirectory() as temporary, patch("main.translate_chunk") as chunk:
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
        with patch("main.time.sleep", side_effect=stop) as sleep:
            with self.assertRaises(main.TranslationCancelled):
                main.run_translation_batch(operation, self.cfg, "test")
        operation.assert_called_once()
        sleep.assert_called_once_with(0.2)

    def test_backoff_increases_and_is_capped(self):
        self.cfg.update(translation_retry_delay_seconds=40, translation_max_retries=3)
        operation = Mock(side_effect=[requests.Timeout()] * 3 + ["ok"])
        with patch("main.time.sleep") as sleep:
            self.assertEqual(main.run_translation_batch(operation, self.cfg, "test"), "ok")
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 160)


if __name__ == "__main__":
    unittest.main()
