import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
from languages import language_code, normalize_output, output_glossary
from source_epub import SourceChapter, translated_source_epub


class LanguageTests(unittest.TestCase):
    def test_aliases_and_unknown_language(self):
        self.assertEqual(language_code("简体中文"), "zh-Hans")
        self.assertEqual(language_code("zh-TW"), "zh-Hant")
        self.assertEqual(language_code("English"), "en")
        with self.assertRaises(ValueError):
            language_code("unknown")

    def test_chinese_is_normalized_in_both_directions(self):
        self.assertEqual(normalize_output("汉语与龙", "繁體中文"), "漢語與龍")
        self.assertEqual(normalize_output("漢語與龍", "簡體中文"), "汉语与龙")
        self.assertEqual(normalize_output("English 漢語", "en"), "English 漢語")

    def test_language_specific_glossary_does_not_leak_chinese(self):
        cfg = {"glossary": {"竜": "龍"}, "glossary_by_language": {"en": {"竜": "dragon"}}}
        self.assertEqual(output_glossary(cfg, "簡體中文"), {"竜": "龙"})
        self.assertEqual(output_glossary(cfg, "en"), {"竜": "dragon"})
        self.assertEqual(output_glossary(cfg, "fr"), {})

    def test_old_translation_cache_is_normalized_without_model_call(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            cfg = {"model": "test", "target_language": "簡體中文", "check_glossary": False}
            episode = main.Episode(url="one", work_title="", episode_title="", paragraphs=["原文"], blocks=[])
            with patch("main.translate_chunk", return_value=["漢語與龍"]):
                self.assertEqual(main.translate_episode(episode, cfg, folder), ["汉语与龙"])
            with patch("main.translate_chunk") as translate:
                self.assertEqual(main.translate_episode(episode, cfg, folder), ["汉语与龙"])
                translate.assert_not_called()

    def test_distinct_output_folders_and_matching_legacy_resume(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            source.write_bytes(b"original")
            cfg = {"output_dir": str(root)}
            folders = [main.translation_work_dir(source, cfg, lang) for lang in ("zh-Hant", "zh-Hans", "en")]
            self.assertEqual(len(set(folders)), 3)
            legacy = root / "book_中文翻譯"
            legacy.mkdir()
            book = main.TranslationBook(source, legacy, {"title": "書"}, "简体中文")
            main.atomic_json(book.path, book.state)
            self.assertEqual(main.translation_work_dir(source, cfg, "簡體中文"), legacy)
            self.assertNotEqual(main.translation_work_dir(source, cfg, "en"), legacy)
            # Equivalent simplified Chinese labels must resume the same book.
            main.TranslationBook(source, legacy, {"title": "書"}, "簡體中文")

    def test_epub_language_and_script_match_selected_output(self):
        with TemporaryDirectory() as temporary:
            for language, title, body, expected in (
                ("en", "Book", "A dragon", "A dragon"),
                ("zh-Hans", "漢語", "漢語與龍", "汉语与龙"),
                ("zh-Hant", "汉语", "汉语与龙", "漢語與龍"),
            ):
                with self.subTest(language=language):
                    chapter = SourceChapter(url="one", title=title, paragraphs=[body],
                                            blocks=[{"type": "text", "text": body}])
                    output = Path(temporary) / f"{language}.epub"
                    translated_source_epub({"title": title}, [chapter], output, language)
                    with zipfile.ZipFile(output) as archive:
                        opf = next(name for name in archive.namelist() if name.endswith(".opf"))
                        self.assertIn(f">{language}</dc:language>", archive.read(opf).decode())
                        content = "\n".join(archive.read(name).decode() for name in archive.namelist()
                                            if name.endswith(".xhtml"))
                        self.assertIn(expected, content)
                    self.assertEqual(chapter.paragraphs, [body])

    def test_language_job_resume_skips_completed_model_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            source.write_bytes(b"original")
            cfg = {"output_dir": str(root), "model": "test", "check_glossary": False}
            chapter = SourceChapter(url="one", title="chapter", paragraphs=["paragraph"],
                                    blocks=[{"type": "text", "text": "paragraph"}])
            with patch("main.extract_epub_chapters", return_value=({"title": "book"}, [chapter])), \
                 patch("main.translate_chunk", side_effect=lambda paragraphs, *_: paragraphs) as translate:
                output = main.translate_epub_language(source, cfg, "en")
                self.assertTrue(output.exists())
                self.assertTrue(output.name.endswith("_en.epub"))
                self.assertEqual(translate.call_count, 3)
                translate.reset_mock()
                self.assertEqual(main.translate_epub_language(source, cfg, "en"), output)
                translate.assert_not_called()

    def test_cancel_before_model_request(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "book.epub"
            source.write_bytes(b"original")
            cfg = {"output_dir": temporary, "_translation_cancelled": lambda: True}
            with patch("main.extract_epub_chapters", return_value=({"title": "book"}, [])), \
                 patch("main.translate_episode") as translate:
                with self.assertRaises(main.TranslationCancelled):
                    main.translate_epub_language(source, cfg, "en")
                translate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
