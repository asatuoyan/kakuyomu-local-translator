import unittest
from unittest.mock import patch

import translator.engine as main
from translator.translation.hy_mt_profile import uses_hy_mt_30b_profile, reference_context_limit


class HyMTProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"model": "hy-mt2-30b:latest", "target_language": "简体中文"}

    def test_reference_limit_is_bounded_and_can_be_disabled(self):
        self.assertEqual(reference_context_limit({}), 256)
        self.assertEqual(reference_context_limit({"context_chars": 100}), 100)
        self.assertEqual(reference_context_limit({"hy_mt_context_chars": 0}), 0)
        self.assertEqual(reference_context_limit({"hy_mt_context_chars": 600}), 600)

    def test_recognizes_local_and_official_names_without_matching_7b(self):
        for name in ("hy-mt2-30b:latest", "hf.co/tencent/Hy-MT2-30B-A3B-GGUF:Q4_K_M",
                     "Hy_MT2_30B_A3B:Q6_K"):
            self.assertTrue(uses_hy_mt_30b_profile({"model": name}))
        for name in (main.DEFAULT_MODEL, "murasaki-14b-q6", "qwen3-30b-a3b", "hy-mt1.5-30b"):
            self.assertFalse(uses_hy_mt_30b_profile({"model": name}))

    @patch("translator.engine.ollama_chat_content", return_value="基础知识")
    def test_uses_30b_sampling_and_short_title_prompt(self, chat):
        self.assertEqual(main.translate_chunk(["基本知識"], self.cfg, "previous"), ["基础知识"])
        payload = chat.call_args.args[0]
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"], {"temperature": 0.7, "top_p": 1.0,
            "top_k": 0, "min_p": 0.0, "repeat_penalty": 1.0, "num_predict": 4096, "num_ctx": 4096})
        self.assertNotIn("previous", payload["messages"][0]["content"])

    @patch("translator.engine.ollama_chat_content", return_value="One.\n\nTwo.")
    def test_terminology_background_and_full_language_names(self, chat):
        self.cfg.update(target_language="en", glossary={"魔王": "Demon King"}, context_chars=10)
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg, "a" * 20), ["One.", "Two."])
        prompt = chat.call_args.args[0]["messages"][0]["content"]
        self.assertIn("魔王 翻译成 Demon King", prompt)
        self.assertIn("翻译为英文", prompt)
        self.assertIn("〖背景信息〗\n" + "a" * 10 + "\n", prompt)
        self.assertTrue(prompt.endswith("<source_text>\n一\n\n二\n</source_text>"))
        self.assertLess(prompt.index("输出要求"), prompt.index("<source_text>\n"))

    @patch("translator.engine.ollama_chat_content")
    def test_length_limit_retries_with_penalty_and_no_context(self, chat):
        chat.side_effect = [RuntimeError("Ollama 输出达到长度限制"), "译文"]
        self.assertEqual(main.translate_chunk(["原文"], self.cfg), ["译文"])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args.args[0]["options"]["repeat_penalty"], 1.05)
