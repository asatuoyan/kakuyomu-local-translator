import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import translator.engine as main
from translator.storage.project_storage import load_translation_state
from translator.translation.translation_workflow import run_workflow, workflow_path
from translator.acquisition.source_epub import SourceChapter


class WorkflowTests(unittest.TestCase):
    def setup_book(self, directory, count=25):
        source = Path(directory) / "novel.epub"
        source.write_bytes(b"original novel")
        cfg = {"output_dir": directory, "model": "test", "check_glossary": False,
               "adaptive_translation_batches": False}
        chapters = [SourceChapter(f"epub://{i}", f"章节{i}", [f"原文{i}"],
                                  blocks=[{"type": "text", "text": f"原文{i}"}]) for i in range(1, count + 1)]
        return source, cfg, chapters

    def test_default_twenty_then_confirmed_full_reuses_preview_and_does_not_autocontinue(self):
        with TemporaryDirectory() as directory:
            source, cfg, chapters = self.setup_book(directory)
            with patch("translator.engine.extract_epub_chapters", return_value=({"title": "小说"}, chapters)), \
                    patch("translator.engine.translate_episode", side_effect=lambda ep, *_: ["译" + t for t in ep.paragraphs]) as translate:
                state = run_workflow(source, cfg, ["zh-Hans"])
                result = state["previews"]["zh-Hans"]
                self.assertEqual(state["stage"], "awaiting_confirmation")
                self.assertFalse(state["confirmed"])
                self.assertEqual(result["chapters"], 20)
                project_path = Path(result["project"]) / "translation-project.json"
                before = load_translation_state(project_path)["chapters"]
                self.assertEqual(len(before), 20)
                self.assertTrue(Path(result["output"]).exists())
                self.assertTrue(Path(result["report"]).exists())
                translate.reset_mock()
                complete = run_workflow(source, cfg, ["zh-Hans"], full=True)
                self.assertEqual(complete["stage"], "complete")
                self.assertTrue(complete["confirmed"])
                self.assertEqual(complete["full_outputs"]["zh-Hans"]["chapters"], 25)
                self.assertEqual(load_translation_state(project_path)["chapters"][:20], before)
                body_calls = [call for call in translate.call_args_list if call.args[0].url in {c.url for c in chapters}]
                self.assertEqual(len(body_calls), 5)
                full_path = Path(complete["full_outputs"]["zh-Hans"]["output"])
                full_before = full_path.read_bytes()
                run_workflow(source, cfg, ["zh-Hans"], preview_count=3)
                self.assertEqual(full_path.read_bytes(), full_before)
                self.assertEqual(len(load_translation_state(project_path)["chapters"]), 25)

    def test_full_requires_preview_for_every_language_and_unchanged_source(self):
        with TemporaryDirectory() as directory:
            source, cfg, chapters = self.setup_book(directory, count=3)
            with self.assertRaisesRegex(ValueError, "试译"):
                run_workflow(source, cfg, ["zh-Hans"], full=True)
            with patch("translator.engine.extract_epub_chapters", return_value=({"title": "小说"}, chapters)), \
                    patch("translator.engine.translate_episode", side_effect=lambda ep, *_: ["译" + t for t in ep.paragraphs]):
                state = run_workflow(source, cfg, ["zh-Hans"])
            self.assertEqual(state["previews"]["zh-Hans"]["chapters"], 3)
            with self.assertRaisesRegex(ValueError, "试译"):
                run_workflow(source, cfg, ["zh-Hans", "en"], full=True)
            source.write_bytes(b"changed novel")
            with self.assertRaisesRegex(ValueError, "试译"):
                run_workflow(source, cfg, ["zh-Hans"], full=True)

    def test_stopped_workflow_records_state_and_can_resume(self):
        with TemporaryDirectory() as directory:
            source, cfg, _ = self.setup_book(directory)
            with patch("translator.engine.translate_epub_language", side_effect=main.TranslationCancelled()):
                with self.assertRaises(main.TranslationCancelled):
                    run_workflow(source, cfg, ["zh-Hans"])
            state = json.loads(workflow_path(source, cfg).read_text(encoding="utf-8"))
            self.assertEqual(state["stage"], "stopped")
            self.assertFalse(state["previews"])
