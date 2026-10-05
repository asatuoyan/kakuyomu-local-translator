import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from first_translation_terms import capture_terms
from glossary_manager import GlossaryEntry, load_project_glossary, save_project_glossary


class FirstTermsTests(unittest.TestCase):
    def test_next_chapter_uses_first_saved_name(self):
        import main
        from source_epub import SourceChapter
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
            with patch("main.extract_epub_chapters", return_value=({"title": "book"}, chapters)), \
                 patch("main.translate_episode", side_effect=translate), \
                 patch("main.ollama_chat_content", return_value=json.dumps({"entries": [
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
            with patch("main.ollama_chat_content", return_value=json.dumps({"entries": entries})):
                result = capture_terms(["アリスとレオン、魔王", "魔王"],
                                       ["艾丽丝和里昂，魔王", "恶魔王"],
                                       {"model": "test", "glossary": {}}, folder)
            self.assertEqual([(e.source, e.target) for e in result], [("レオン", "里昂")])
            self.assertEqual({e.source: e.target for e in load_project_glossary(folder)},
                             {"アリス": "爱丽丝", "レオン": "里昂"})

    def test_invalid_extraction_does_not_save_partial_terms(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("main.ollama_chat_content", return_value="invalid"):
                with self.assertRaises(ValueError):
                    capture_terms(["レオン"], ["里昂"], {"model": "test"}, folder)
            self.assertFalse((Path(folder) / "glossary.json").exists())
