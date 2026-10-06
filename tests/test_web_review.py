import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from translator.engine import retranslate_project
from translator.storage.project_storage import atomic_json, load_translation_state
from translator.ui.web_app import Application


class WebReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "book"
        self.manifest = self.folder / "translation-project.json"
        self.cfg = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        self.cfg.update(output_dir=self.tmp.name, model="test")
        self.app = Application(self.cfg)
        self.chapters = [
            {"title": "Same title", "source_paragraphs": ["アリスが来た", "そのまま"],
             "paragraphs": ["旧名字来了", "保留这一段"]},
            {"title": "Same title", "source_paragraphs": ["アリスが帰った"], "paragraphs": ["旧名字回去了"]}]
        self.write()
        self.app.write_terms({"project": "book", "entries": [{"source": "アリス", "target": "旧名字"}]})

    def write(self):
        atomic_json(self.manifest, {"metadata": {"title": "Book"}, "language": "zh-Hans",
                                    "chapters": self.chapters})

    def change_terms(self):
        self.app.write_terms({"project": "book", "merge": True,
                              "entries": [{"source": "アリス", "target": "新名字"}]})

    def test_changed_terms_persist_across_restart_and_find_only_matching_originals(self):
        self.change_terms()
        self.chapters[0]["paragraphs"][1] = "旧名字出现在其他原文段落"
        self.write()
        restarted = Application(self.cfg)
        report = restarted.review({"project": "book", "mode": "changes"})
        self.assertEqual([(r["chapter_index"], r["paragraph"]) for r in report["items"]], [(1, 1), (2, 1)])
        self.assertTrue(all("旧名字 → 新名字" in r["reasons"][0] for r in report["items"]))
        self.assertEqual(report["unavailable"], [])
        self.app.write_terms({"project": "book", "merge": True,
                              "entries": [{"source": "アリス", "target": "旧名字"}]})
        self.assertEqual(self.app.review({"project": "book", "mode": "changes"})["items"], [])

    def test_review_distinguishes_missing_originals_and_shows_exact_text(self):
        self.change_terms()
        self.chapters[0]["source_paragraphs"][0] = "  アリスが来た  "
        self.chapters.append({"title": "Missing", "paragraphs": ["only translation"]})
        self.write()
        before = self.manifest.read_bytes()
        report = self.app.review({"project": "book"})
        self.assertEqual(len(report["items"]), 2)
        self.assertEqual(report["items"][0]["original"], "  アリスが来た  ")
        self.assertEqual(report["unavailable"][0]["chapter"], "Missing")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_multi_selection_retranslates_exact_paragraphs_with_duplicate_chapter_titles(self):
        self.change_terms()
        report = self.app.review({"project": "book", "mode": "changes"})
        locations = [(r["chapter_index"], r["paragraph"], r["original"], r["translation"]) for r in report["items"]]
        with patch("translator.engine.translate_chunk", side_effect=[["新名字来了"], ["新名字回去了"]]) as translate:
            output = retranslate_project(self.folder, self.cfg, [], paragraph_locations=locations + [locations[0]])
        self.assertEqual(translate.call_count, 2)
        self.assertTrue(output.exists())
        updated = load_translation_state(self.manifest)
        self.assertEqual(updated["chapters"][0]["paragraphs"], ["新名字来了", "保留这一段"])
        self.assertEqual(updated["chapters"][1]["paragraphs"], ["新名字回去了"])
        self.assertEqual(self.app.review({"project": "book", "mode": "changes"})["items"], [])

    def test_stale_selection_fails_before_any_model_request_or_write(self):
        locations = [(1, 1, "アリスが来た", "旧名字来了"), (2, 1, "アリスが帰った", "outdated")]
        before = self.manifest.read_bytes()
        with patch("translator.engine.translate_chunk") as translate:
            with self.assertRaisesRegex(ValueError, "内容已变化"):
                retranslate_project(self.folder, self.cfg, [], paragraph_locations=locations)
        translate.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_selective_retranslation_keeps_unrelated_chapter_without_originals(self):
        self.chapters.append({"title": "Missing", "paragraphs": ["saved text"]})
        self.write()
        stages = []
        with patch("translator.engine.translate_chunk", return_value=["updated"]):
            retranslate_project(self.folder, {**self.cfg, "_task_stage": lambda *args: stages.append(args)}, [],
                                paragraph_locations=[(1, 1, "アリスが来た", "旧名字来了")])
        self.assertEqual(load_translation_state(self.manifest)["chapters"][2]["paragraphs"], ["saved text"])
        self.assertEqual(stages, [("translation", "Same title · 第 1 段 · 局部重译")])

    def test_background_task_reserves_project_and_rejects_changed_glossary(self):
        report = self.app.review({"project": "book"})
        self.change_terms()
        with self.assertRaisesRegex(ValueError, "术语已变化"):
            self.app.retranslate({"project": "book", "model": "test", "term_revision": report["term_revision"],
                                  "items": [{"chapter_index": 1, "paragraph": 1, "original": "x", "translation": "x"}]})
        report = self.app.review({"project": "book"})
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set()
            release.wait(5)
            finished.set()
            return self.folder / "updated.epub"
        with patch("translator.engine.ensure_model"), patch("translator.engine.retranslate_project", side_effect=run):
            self.app.retranslate({"project": "book", "model": "test", "term_revision": report["term_revision"],
                                  "items": report["items"]})
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ValueError, "停止"):
                    self.app.write_terms({"project": "book", "entries": []})
                with self.assertRaisesRegex(ValueError, "停止"):
                    self.app.review({"project": "book"})
                self.assertEqual(self.app.task["kind"], "review")
            finally:
                release.set()
                self.assertTrue(finished.wait(5))


if __name__ == "__main__":
    unittest.main()
