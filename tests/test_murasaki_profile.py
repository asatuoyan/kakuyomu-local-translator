import unittest
from unittest.mock import patch

import translator.engine as main
from translator.translation.murasaki_profile import translation_only, uses_murasaki_profile, ThinkingBudgetExceeded


class MurasakiProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"model": "murasaki-8b-q6:latest", "target_language": "简体中文"}

    @patch("translator.engine.ollama_chat_content", return_value="<think>分析</think>基础知识")
    def test_title_uses_short_prompt_and_excludes_previous_translation(self, chat):
        self.assertEqual(main.translate_chunk(["基本知識"], self.cfg, "旧译文"), ["基础知识"])
        payload = chat.call_args.args[0]
        self.assertIs(payload["think"], False)
        self.assertIn("独立短句", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["content"], "请翻译：\n<source_text>\n基本知識\n</source_text>")
        self.assertIn("译文使用简体中文", payload["messages"][0]["content"])
        self.assertNotIn("旧译文", str(payload["messages"]))
        self.assertEqual(payload["options"]["num_predict"], 256)

    @patch("translator.engine.ollama_chat_content")
    def test_thinking_budget_falls_back_without_repeating_thinking(self, chat):
        chat.side_effect = [ThinkingBudgetExceeded("limit"), "译文一\n\n译文二"]
        self.cfg["model"] = "murasaki-14b-q6:latest"
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg), ["译文一", "译文二"])
        self.assertTrue(chat.call_args_list[0].args[0]["think"])
        self.assertFalse(chat.call_args_list[1].args[0]["think"])
        self.assertNotIn("先分析", chat.call_args_list[1].args[0]["messages"][0]["content"])
        self.assertEqual(chat.call_count, 2)

    @patch("translator.engine.requests.post")
    def test_thinking_stream_limit_closes_response(self, post):
        import json
        from tests.test_main import FakeResponse
        response = FakeResponse([json.dumps({"message": {"thinking": "x" * 1025}})])
        post.return_value = response
        with self.assertRaises(ThinkingBudgetExceeded):
            main._read_ollama_content({}, {"ollama_url": "http://localhost", "_thinking_char_limit": 1024})
        self.assertTrue(response.closed)

    @patch("translator.engine.ollama_chat_content", return_value="译文一\n\n译文二")
    def test_body_reserves_thinking_budget_and_preserves_paragraphs(self, chat):
        self.cfg["glossary"] = {"一": "译文一"}
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg), ["译文一", "译文二"])
        payload = chat.call_args.args[0]
        self.assertIn("轻小说", payload["messages"][0]["content"])
        self.assertIn("〖术语表〗", payload["messages"][0]["content"])
        self.assertEqual(payload["options"]["num_predict"], 4096)

    def test_unfinished_reasoning_is_not_saved_as_translation(self):
        with self.assertRaisesRegex(RuntimeError, "没有完整译文"):
            translation_only("<think>still thinking")
        self.assertEqual(translation_only("analysis</think>译文"), "译文")
        self.assertEqual(translation_only("<THINK >分析</THINK >译文"), "译文")

    @patch("translator.engine.ollama_chat_content")
    def test_echo_after_reasoning_is_retried_without_saving_instructions(self, chat):
        chat.side_effect = ["<think>分析</think>请翻译：\n<source_text>\n译文\n</source_text>", "干净译文"]
        self.assertEqual(main.translate_chunk(["原文"], self.cfg), ["干净译文"])
        self.assertEqual(chat.call_count, 2)

    @patch("translator.engine.ollama_chat_content")
    def test_persistent_system_prompt_echo_fails(self, chat):
        chat.return_value = "你负责日文 ACGN 独立短句的中文翻译。\n译文"
        with self.assertRaisesRegex(RuntimeError, "未保存"):
            main.translate_chunk(["原文"], self.cfg)
        self.assertEqual(chat.call_count, 2)

    @patch("translator.engine.ollama_chat_content", return_value="繁體譯文")
    def test_traditional_chinese_is_explicit_and_glossary_stays_in_system(self, chat):
        self.cfg.update(target_language="繁体中文", glossary={"勇者": "勇者"})
        self.assertEqual(main.translate_chunk(["勇者"], self.cfg), ["繁體譯文"])
        messages = chat.call_args.args[0]["messages"]
        self.assertIn("译文使用繁体中文", messages[0]["content"])
        self.assertIn("〖术语表〗", messages[0]["content"])
        self.assertNotIn("〖术语表〗", messages[1]["content"])

    def test_profile_does_not_claim_multilingual_or_4b_support(self):
        self.cfg["target_language"] = "en"
        self.assertFalse(uses_murasaki_profile(self.cfg))
        self.cfg.update(model="murasaki-4b:latest", target_language="简体中文")
        self.assertFalse(uses_murasaki_profile(self.cfg))

    @patch("translator.engine.ollama_chat_content", return_value="勇者")
    def test_only_matching_glossary_terms_are_sent(self, chat):
        self.cfg["glossary"] = {"勇者": "勇者", "魔王": "恶魔王", "勇": "勇"}
        main.translate_chunk(["勇者"], self.cfg)
        system = chat.call_args.args[0]["messages"][0]["content"]
        self.assertNotIn("魔王", system)
        self.assertIn("勇者", system)

    @patch("translator.engine.ollama_chat_content")
    def test_alignment_failure_retries_with_adjusted_sampling(self, chat):
        chat.side_effect = ["合并译文", "译一", "译二"]
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg), ["译一", "译二"])
        options = chat.call_args.args[0]["options"]
        self.assertEqual(options["temperature"], .5)
        self.assertEqual(options["repeat_penalty"], 1.1)
