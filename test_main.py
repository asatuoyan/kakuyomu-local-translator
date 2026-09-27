import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


class OllamaChatContentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "ollama_url": "http://127.0.0.1:11434/",
            "request_timeout_seconds": 600,
        }

    @patch("main.requests.post")
    def test_assembles_streamed_content_and_closes_response(self, post):
        response = FakeResponse([
            b'{"message":{"content":"{\\"translations\\":["}}',
            b'{"message":{"content":"\\"translated\\"]}"},"done":true}',
        ])
        post.return_value = response

        content = main.ollama_chat_content({"model": "test"}, self.cfg)

        self.assertEqual(content, '{"translations":["translated"]}')
        post.assert_called_once_with(
            "http://127.0.0.1:11434/api/chat",
            json={"model": "test", "stream": True},
            stream=True,
            timeout=600,
        )
        self.assertTrue(response.closed)

    @patch("main.requests.post")
    def test_reports_streamed_ollama_error_and_closes_response(self, post):
        response = FakeResponse([b'{"error":"model runner stopped"}'])
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "model runner stopped"):
            main.ollama_chat_content({"model": "test"}, self.cfg)

        self.assertTrue(response.closed)


class TranslationBatchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "ollama_url": "http://127.0.0.1:11434",
            "model": "test",
            "target_language": "繁體中文",
        }

    def test_make_batches_limits_paragraph_count(self):
        batches = main.make_batches(["短句"] * 81, limit=2200, max_items=40)

        self.assertEqual([len(batch) for batch in batches], [40, 40, 1])

    @patch("main.ollama_chat_content")
    def test_incomplete_translation_is_retried_in_smaller_batches(self, chat):
        chat.side_effect = [
            '譯一\n\n譯二\n\n譯三',
            '譯一\n\n譯二',
            '譯三\n\n譯四',
        ]

        result = main.translate_chunk(["一", "二", "三", "四"], self.cfg)

        self.assertEqual(result, ["譯一", "譯二", "譯三", "譯四"])
        self.assertEqual(chat.call_count, 3)

    @patch("main.ollama_chat_content")
    def test_token_repeat_error_splits_translation_batch(self, chat):
        chat.side_effect = [
            RuntimeError("Ollama 生成失敗：prediction aborted, token repeat limit reached"),
            '譯一\n\n譯二',
            '譯三\n\n譯四',
        ]

        result = main.translate_chunk(["一", "二", "三", "四"], self.cfg)

        self.assertEqual(result, ["譯一", "譯二", "譯三", "譯四"])
        self.assertEqual(chat.call_count, 3)
        self.assertIn("譯一", chat.call_args_list[2].args[0]["messages"][0]["content"])

    @patch("main.ollama_chat_content")
    def test_token_repeat_error_retries_single_paragraph_once(self, chat):
        chat.side_effect = [
            RuntimeError("Ollama 生成失敗：prediction aborted, token repeat limit reached"),
            '譯一',
        ]

        self.assertEqual(main.translate_chunk(["一"], self.cfg, "先前譯文"), ["譯一"])
        self.assertEqual(chat.call_count, 2)
        self.assertGreater(chat.call_args.args[0]["options"]["temperature"], 0.2)
        self.assertNotIn("先前譯文", chat.call_args.args[0]["messages"][0]["content"])

    @patch("main.ollama_chat_content")
    def test_repeated_single_paragraph_error_is_not_retried_forever(self, chat):
        chat.side_effect = RuntimeError("Ollama 生成失敗：prediction aborted, token repeat limit reached")

        with self.assertRaisesRegex(RuntimeError, "token repeat limit reached"):
            main.translate_chunk(["一"], self.cfg)

        self.assertEqual(chat.call_count, 2)

    @patch("main.ollama_chat_content")
    def test_other_ollama_error_is_not_retried(self, chat):
        chat.side_effect = RuntimeError("Ollama 生成失敗：model not found")

        with self.assertRaisesRegex(RuntimeError, "model not found"):
            main.translate_chunk(["一", "二"], self.cfg)

        self.assertEqual(chat.call_count, 1)




class ModelCheckTests(unittest.TestCase):
    @patch("main.installed_models", return_value={"hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"})
    @patch("builtins.input", side_effect=RuntimeError("lost sys.stdin"))
    def test_gui_missing_model_reports_available_name_without_prompt(self, prompt, _models):
        cfg = {"model": "aya-expanse-8b-GGUF:Q5_K_M"}

        with self.assertRaisesRegex(RuntimeError, "hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"):
            main.ensure_model(cfg, interactive=False)

        prompt.assert_not_called()

    @patch("main.installed_models", return_value={"hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"})
    @patch("builtins.input", side_effect=RuntimeError("lost sys.stdin"))
    def test_gui_installed_model_never_prompts(self, prompt, _models):
        cfg = {"model": "hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"}

        self.assertTrue(main.ensure_model(cfg, interactive=False))
        prompt.assert_not_called()


class TranslationBookTests(unittest.TestCase):
    def test_ten_chapter_parts_hundred_chapter_merges_and_final_merge(self):
        with TemporaryDirectory() as temporary:
            work_dir = Path(temporary)
            source = work_dir / "original.epub"
            source.write_bytes(b"source")
            metadata = {"title": "小說", "author": "作者"}
            calls = []

            def write_epub(_metadata, chapters, output, _language):
                calls.append((len(chapters), chapters[0].title, chapters[-1].title))
                output.write_bytes(b"epub")

            with patch("main.translated_source_epub", side_effect=write_epub):
                book = main.TranslationBook(source, work_dir, metadata, "繁體中文")
                for index in range(1, 206):
                    original = main.SourceChapter(url=f"epub://{index}", title=str(index),
                                                  paragraphs=[f"原文{index}"], blocks=[])
                    translated = main.SourceChapter(url=original.url, title=str(index),
                                                    paragraphs=[f"譯文{index}"], blocks=[])
                    book.save(index, original, translated)
                    book.checkpoint(index, 205)
                self.assertEqual(calls[:2], [(10, "1", "10"), (10, "11", "20")])
                self.assertIn((100, "1", "100"), calls)
                self.assertIn((200, "1", "200"), calls)
                self.assertIn((5, "201", "205"), calls)
                self.assertTrue((work_dir / "小說_繁中_0001-0010.epub").exists())

                resumed = main.TranslationBook(source, work_dir, metadata, "繁體中文")
                self.assertTrue(resumed.completed(205, original))
                self.assertEqual(resumed.checkpoint(200, 205), [])
                output = resumed.finish(205)
                self.assertTrue(output.exists())
                self.assertEqual(calls[-1], (205, "1", "205"))

    def test_different_source_is_rejected_before_reusing_chapters(self):
        with TemporaryDirectory() as temporary:
            work_dir = Path(temporary)
            source = work_dir / "original.epub"
            source.write_bytes(b"source")
            main.TranslationBook(source, work_dir, {"title": "小說"}, "繁體中文").save(
                1, main.SourceChapter(url="one", title="1", paragraphs=["原文"], blocks=[]),
                main.SourceChapter(url="one", title="1", paragraphs=["譯文"], blocks=[]),
            )
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "原文"):
                main.TranslationBook(source, work_dir, {"title": "小說"}, "繁體中文")


class ExistingProjectTests(unittest.TestCase):
    def test_reimport_same_base_keeps_translated_chapters(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.epub"
            source.write_bytes(b"existing book")
            work_dir = root / "小說"
            work_dir.mkdir()
            (work_dir / "base-original.epub").write_bytes(source.read_bytes())
            project = {"base_epub": "base-original.epub", "chapters": [{"title": "舊章"}]}
            main.atomic_json(work_dir / "project.json", project)
            with patch("main.select_epub_file", return_value=source), \
                 patch("main.inspect_epub", return_value={"title": "小說"}), \
                 patch("main.save_last_project"), \
                 patch("main.create_project_from_epub") as create:
                self.assertEqual(main.import_epub({"output_dir": str(root)}), work_dir)
            create.assert_not_called()
            self.assertEqual(main.load_json(work_dir / "project.json", {}), project)


if __name__ == "__main__":
    unittest.main()
