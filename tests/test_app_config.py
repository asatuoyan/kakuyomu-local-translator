import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from translator.config import APP_DIR, DEFAULT_MODEL, load_config, update_config


class AppConfigTests(unittest.TestCase):
    def test_partial_config_keeps_custom_model_and_resolves_paths(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"model": "custom:latest", "output_dir": "books"}), encoding="utf-8")
            config = load_config(path, APP_DIR)
            self.assertEqual(config["model"], "custom:latest")
            self.assertEqual(config["output_dir"], str((APP_DIR / "books").resolve()))
            self.assertEqual(config["translation_chunk_chars"], 2200)
            self.assertEqual(config["ui_theme"], "light")

    def test_targeted_update_preserves_relative_paths_and_unknown_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"output_dir": "books", "legacy_option": 7}), encoding="utf-8")
            update_config({"model": "custom:latest", "ui_theme": "dark"}, path, APP_DIR)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["output_dir"], "books")
            self.assertEqual(saved["legacy_option"], 7)
            self.assertEqual(saved["model"], "custom:latest")
            self.assertEqual(saved["ui_theme"], "dark")

    def test_missing_model_uses_default_and_invalid_values_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"model": " "}', encoding="utf-8")
            self.assertEqual(load_config(path, APP_DIR)["model"], DEFAULT_MODEL)
            for changes in ({"translation_chunk_chars": 0}, {"request_timeout_seconds": -1},
                            {"ollama_url": "not-a-url"}, {"_translation_cancelled": True}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    update_config(changes, path, APP_DIR)


if __name__ == "__main__":
    unittest.main()
