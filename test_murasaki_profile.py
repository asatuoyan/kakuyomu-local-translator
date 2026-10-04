import unittest
from unittest.mock import patch

import main
from murasaki_profile import translation_only, uses_murasaki_profile, ThinkingBudgetExceeded


class MurasakiProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"model": "murasaki-8b-q6:latest", "target_language": "简体中文"}

    @patch("main.ollama_chat_content", return_value="<think>分析</think>基础知识")
    def test_title_uses_short_prompt_and_excludes_previous_translation(self, chat):
        self.assertEqual(main.translate_chunk(["基本知識"], self.cfg, "旧译文"), ["基础知识"])
        payload = chat.call_args.args[0]
        self.assertIs(payload["think"], False)
        self.assertIn("独立短句", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["content"], "请翻译：\n基本知識")
        self.assertEqual(payload["options"]["num_predict"], 256)

    @patch("main.ollama_chat_content")
    def test_thinking_budget_falls_back_without_repeating_thinking(self, chat):
        chat.side_effect = [ThinkingBudgetExceeded("limit"), "译文一\n\n译文二"]
        self.cfg["model"] = "murasaki-14b-q6:latest"
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg), ["译文一", "译文二"])
        self.assertTrue(chat.call_args_list[0].args[0]["think"])
        self.assertFalse(chat.call_args_list[1].args[0]["think"])
        self.assertNotIn("先分析", chat.call_args_list[1].args[0]["messages"][0]["content"])
        self.assertEqual(chat.call_count, 2)

    @patch("main.requests.post")
    def test_thinking_stream_limit_closes_response(self, post):
        import json
        from test_main import FakeResponse
        response = FakeResponse([json.dumps({"message": {"thinking": "x" * 1025}})])
        post.return_value = response
        with self.assertRaises(ThinkingBudgetExceeded):
            main._read_ollama_content({}, {"ollama_url": "http://localhost", "_thinking_char_limit": 1024})
        self.assertTrue(response.closed)

    @patch("main.ollama_chat_content", return_value="译文一\n\n译文二")
    def test_body_reserves_thinking_budget_and_preserves_paragraphs(self, chat):
        self.cfg["glossary"] = {"魔王": "魔王"}
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg), ["译文一", "译文二"])
        payload = chat.call_args.args[0]
        self.assertIn("轻小说", payload["messages"][0]["content"])
        self.assertIn("〖术语表〗", payload["messages"][0]["content"])
        self.assertEqual(payload["options"]["num_predict"], 4096)

    def test_unfinished_reasoning_is_not_saved_as_translation(self):
        with self.assertRaisesRegex(RuntimeError, "没有完整译文"):
            translation_only("<think>still thinking")
        self.assertEqual(translation_only("analysis</think>译文"), "译文")

    def test_profile_does_not_claim_multilingual_or_4b_support(self):
        self.cfg["target_language"] = "en"
        self.assertFalse(uses_murasaki_profile(self.cfg))
        self.cfg.update(model="murasaki-4b:latest", target_language="简体中文")
        self.assertFalse(uses_murasaki_profile(self.cfg))
