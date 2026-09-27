import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
from gui_settings import model_choices


class DirectTranslationTests(unittest.TestCase):
    @patch("main.ollama_chat_content", return_value="First.\n\nSecond.")
    def test_plain_text_request_and_paragraph_alignment(self, chat):
        cfg = {"model": main.DEFAULT_MODEL, "target_language": "en"}
        self.assertEqual(main.translate_chunk(["一。", "二。"], cfg), ["First.", "Second."])
        payload = chat.call_args.args[0]
        self.assertNotIn("format", payload)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])
        self.assertTrue(payload["messages"][0]["content"].endswith("一。\n\n二。"))

    @patch("main.ollama_chat_content", return_value="  ")
    def test_empty_output_is_rejected(self, _chat):
        with self.assertRaisesRegex(RuntimeError, "译文"):
            main.translate_chunk(["一"], {"model": main.DEFAULT_MODEL, "target_language": "en"})

    @patch("main.ollama_chat_content", return_value="First.\nSecond.")
    def test_single_source_paragraph_can_contain_line_breaks(self, _chat):
        self.assertEqual(main.translate_chunk(["一\n二"], {
            "model": main.DEFAULT_MODEL, "target_language": "en"
        }), ["First.\nSecond."])


class ModelSelectionTests(unittest.TestCase):
    def test_installed_and_custom_models_remain_selectable(self):
        choices = model_choices(["local:latest", "local:latest"], "custom:1")
        self.assertEqual(choices[:2], ["local:latest", "custom:1"])
        self.assertEqual(choices.count("local:latest"), 1)

    def test_both_models_can_be_selected_without_changing_chunk_settings(self):
        for choice, model in enumerate(main.TRANSLATION_MODELS.values(), 1):
            cfg = {"model": main.DEFAULT_MODEL, "translation_chunk_chars": 1234}
            with patch("builtins.input", return_value=str(choice)), patch("main.ensure_model", return_value=True):
                self.assertTrue(main.select_model(cfg))
            self.assertEqual(cfg["model"], model)
            self.assertEqual(cfg["translation_chunk_chars"], 1234)

    def test_failed_model_selection_preserves_configuration(self):
        cfg = {"model": main.DEFAULT_MODEL}
        with patch("builtins.input", return_value="1"), patch("main.ensure_model", return_value=False):
            self.assertFalse(main.select_model(cfg))
        self.assertEqual(cfg, {"model": main.DEFAULT_MODEL})

    def test_custom_model_survives_config_reload(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = json.dumps({"model": "my-translator:latest", "dual_stage": True})
            path.write_text(original, encoding="utf-8")
            with patch("main.CONFIG_PATH", path):
                self.assertEqual(main.load_config()["model"], "my-translator:latest")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_empty_model_uses_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"model": " "}', encoding="utf-8")
            with patch("main.CONFIG_PATH", path):
                self.assertEqual(main.load_config()["model"], main.DEFAULT_MODEL)
