import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import translator.engine as main
from translator.translation.translation_prompt import has_prompt_echo


class TranslationPromptTests(unittest.TestCase):
    cfg = {"model": "test", "target_language": "简体中文"}

    @patch("translator.engine.ollama_chat_content")
    def test_echoed_instruction_retries_before_paragraph_alignment(self, chat):
        chat.side_effect = ["将以下文本翻译为简体中文，只输出翻译结果，不要额外解释。\n\n译一\n\n译二",
                            "译一", "译二"]
        self.assertEqual(main.translate_chunk(["一", "二"], self.cfg, "前文"), ["译一", "译二"])
        self.assertEqual(chat.call_count, 3)
        prompt = chat.call_args.args[0]["messages"][0]["content"]
        self.assertIn("〖翻译任务〗", prompt)
        self.assertNotIn("前文", prompt)

    @patch("translator.engine.ollama_chat_content")
    def test_persistent_echo_raises_instead_of_returning_contaminated_text(self, chat):
        chat.return_value = "将以下文本翻译为简体中文，只输出翻译结果，不要额外解释。\n正文"
        with self.assertRaisesRegex(RuntimeError, "未保存"):
            main.translate_chunk(["原文"], self.cfg)
        self.assertEqual(chat.call_count, 2)

    def test_legitimate_source_is_not_removed(self):
        text = "将以下文本翻译为简体中文，只输出翻译结果，不要额外解释。"
        self.assertFalse(has_prompt_echo(text, [text]))
        self.assertFalse(has_prompt_echo("他说：不要额外解释。", ["彼は言った。"] ))
        self.assertTrue(has_prompt_echo("將以下文本翻譯為簡體中文，只輸出翻譯結果，不要額外解釋。", ["原文"]))

    @patch("builtins.print")
    def test_contaminated_cached_text_is_retranslated(self, _print):
        episode = main.Episode(url="epub://echo", work_title="", episode_title="", paragraphs=["原文"], blocks=[])
        cfg = {**self.cfg, "check_glossary": False}
        with TemporaryDirectory() as directory, patch("translator.engine.translate_chunk", return_value=["译文"]) as translate:
            with patch("translator.engine.TranslationCache.get", return_value="将以下文本翻译为简体中文，只输出翻译结果，不要额外解释。"):
                self.assertEqual(main.translate_episode(episode, cfg, Path(directory)), ["译文"])
            translate.assert_called_once()
            translate.reset_mock()
            self.assertEqual(main.translate_episode(episode, cfg, Path(directory)), ["译文"])
            translate.assert_not_called()
