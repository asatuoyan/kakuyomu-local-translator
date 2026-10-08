import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from translator.glossary.first_terms import capture_terms
from translator.glossary.manager import GlossaryEntry, load_project_glossary, save_project_glossary


class FirstTermsTests(unittest.TestCase):
    def test_context_limit_splits_extraction(self):
        from translator.engine import OllamaContextLimitExceeded
        with tempfile.TemporaryDirectory() as folder, patch("translator.engine.ollama_chat_content") as chat:
            chat.side_effect = [OllamaContextLimitExceeded("context"),
                                '{"entries":[]}', '{"entries":[]}']
            capture_terms(["レオン", "アリス"], ["里昂", "爱丽丝"], {"model": "test"}, folder)
            self.assertEqual([len(json.loads(call.args[0]["messages"][1]["content"]))
                              for call in chat.call_args_list], [2, 1, 1])

    def test_output_limit_splits_extraction_without_saving_truncated_json(self):
        from translator.engine import OllamaOutputLimitExceeded
        with tempfile.TemporaryDirectory() as folder, patch("translator.engine.ollama_chat_content") as chat:
            chat.side_effect = [OllamaOutputLimitExceeded("limit"),
                                '{"entries":[{"source":"レオン","target":"里昂"}]}',
                                '{"entries":[]}']
            result = capture_terms(["レオン", "アリス"], ["里昂", "爱丽丝"],
                                   {"model": "test"}, folder)
            self.assertEqual([(entry.source, entry.target) for entry in result], [("レオン", "里昂")])
            self.assertEqual([len(json.loads(call.args[0]["messages"][1]["content"]))
                              for call in chat.call_args_list], [2, 1, 1])
            self.assertFalse(chat.call_args.args[0]["think"])
            self.assertEqual(chat.call_args.args[1]["_output_label"], "术语提取")

    def test_single_pair_output_limit_has_bounded_retry(self):
        from translator.engine import OllamaOutputLimitExceeded
        with tempfile.TemporaryDirectory() as folder, patch(
                "translator.engine.ollama_chat_content", side_effect=OllamaOutputLimitExceeded("limit")) as chat:
            with self.assertRaises(OllamaOutputLimitExceeded):
                capture_terms(["レオン"], ["里昂"], {"model": "test"}, folder)
            self.assertEqual([call.args[0]["options"]["num_predict"]
                              for call in chat.call_args_list], [1024, 2048])
            self.assertFalse((Path(folder) / "glossary-extraction.json").exists())

    def test_successful_and_empty_extraction_are_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as folder, patch("translator.engine.ollama_chat_content", return_value='{"entries": []}') as chat:
            cfg = {"model": "test"}
            capture_terms(["レオン"], ["里昂"], cfg, folder)
            capture_terms(["レオン"], ["里昂"], cfg, folder)
            self.assertEqual(chat.call_count, 1)
            capture_terms(["レオン"], ["莱昂"], cfg, folder)
            self.assertEqual(chat.call_count, 2)
            capture_terms(["レオン"], ["莱昂"], {"model": "other"}, folder)
            self.assertEqual(chat.call_count, 3)

    def test_failure_does_not_mark_extraction_complete(self):
        with tempfile.TemporaryDirectory() as folder, patch("translator.engine.ollama_chat_content", side_effect=['invalid', '{"entries": []}']) as chat:
            with self.assertRaises(ValueError):
                capture_terms(["レオン"], ["里昂"], {"model": "test"}, folder)
            self.assertFalse((Path(folder) / "glossary-extraction.json").exists())
            capture_terms(["レオン"], ["里昂"], {"model": "test"}, folder)
            self.assertEqual(chat.call_count, 2)

    def test_context_is_explicit_and_long_paragraphs_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {"model": "test", "hy_mt_num_ctx": 8192}
            original, translation = "あ" * 7000, "字" * 8000
            with patch("translator.engine.ollama_chat_content", return_value='{"entries": []}') as chat:
                capture_terms([original], [translation], cfg, folder)
            self.assertGreater(chat.call_count, 1)
            source_pieces, target_pieces = [], []
            for call in chat.call_args_list:
                payload = call.args[0]
                self.assertEqual(payload["options"]["num_ctx"], 8192)
                self.assertEqual(payload["options"]["num_predict"], 1024)
                pairs = json.loads(payload["messages"][1]["content"])
                self.assertLessEqual(sum(len(p["source"]) + len(p["translation"]) for p in pairs), 2528)
                source_pieces.extend(p["source"] for p in pairs)
                target_pieces.extend(p["translation"] for p in pairs)
            self.assertGreaterEqual(sum(map(len, source_pieces)), len(original))
            self.assertGreaterEqual(sum(map(len, target_pieces)), len(translation))

    def test_glossary_context_can_be_configured_independently(self):
        with tempfile.TemporaryDirectory() as folder, patch("translator.engine.ollama_chat_content", return_value='{"entries": []}') as chat:
            capture_terms(["レオン"], ["里昂"], {"model": "test", "hy_mt_num_ctx": 8192,
                                                   "glossary_num_ctx": 4096}, folder)
            self.assertEqual(chat.call_args.args[0]["options"]["num_ctx"], 4096)

    def test_next_chapter_uses_first_saved_name(self):
        import translator.engine as main
        from translator.acquisition.source_epub import SourceChapter
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "book.epub"
            source.write_bytes(b"source")
            chapters = [SourceChapter(url=str(i), title="chapter", paragraphs=["レオン"],
                                      blocks=[{"type": "text", "text": "レオン"}]) for i in range(2)]
            observed = []
            def translate(episode, cfg, work_dir):
                if episode.paragraphs == ["レオン"]:
                    observed.append(dict(cfg["glossary"]))
                    return ["里昂"]
                return episode.paragraphs
            cfg = {"output_dir": folder, "model": "test", "adaptive_translation_batches": False,
                   "_capture_first_terms": True}
            with patch("translator.engine.extract_epub_chapters", return_value=({"title": "book"}, chapters)), \
                 patch("translator.engine.translate_episode", side_effect=translate), \
                 patch("translator.engine.ollama_chat_content", return_value=json.dumps({"entries": [
                     {"source": "レオン", "target": "里昂"}]})):
                output = main.translate_epub_language(source, cfg, "zh-Hans")
            self.assertTrue(output.exists())
            self.assertNotIn("レオン", observed[0])
            self.assertEqual(observed[1]["レオン"], "里昂")

    def test_first_alignment_and_existing_names_win(self):
        with tempfile.TemporaryDirectory() as folder:
            save_project_glossary(folder, [GlossaryEntry("アリス", "爱丽丝")])
            entries = [{"source": "アリス", "target": "艾丽丝"},
                       {"source": "レオン", "target": "里昂"},
                       {"source": "魔王", "target": "恶魔王"},
                       {"source": "架空", "target": "里昂"}]
            with patch("translator.engine.ollama_chat_content", return_value=json.dumps({"entries": entries})):
                result = capture_terms(["アリスとレオン、魔王", "魔王"],
                                       ["艾丽丝和里昂，魔王", "恶魔王"],
                                       {"model": "test", "glossary": {}}, folder)
            self.assertEqual([(e.source, e.target) for e in result], [("レオン", "里昂")])
            self.assertEqual({e.source: e.target for e in load_project_glossary(folder)},
                             {"アリス": "爱丽丝", "レオン": "里昂"})

    def test_invalid_extraction_does_not_save_partial_terms(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("translator.engine.ollama_chat_content", return_value="invalid"):
                with self.assertRaises(ValueError):
                    capture_terms(["レオン"], ["里昂"], {"model": "test"}, folder)
            self.assertFalse((Path(folder) / "glossary.json").exists())
