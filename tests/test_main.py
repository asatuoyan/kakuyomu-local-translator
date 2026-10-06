import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import translator.engine as main


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
    @patch("translator.engine.requests.post")
    def test_http_error_preserves_ollama_context_details_and_closes_response(self, post):
        import requests
        from unittest.mock import Mock
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":"request (4527 tokens) exceeds the available context size (4096 tokens)"}'
        response.close = Mock()
        post.return_value = response
        with self.assertRaisesRegex(requests.HTTPError, "4527 tokens") as raised:
            main._read_ollama_content({"model": "test"}, self.cfg)
        self.assertIs(raised.exception.response, response)
        response.close.assert_called_once()

    @patch("translator.engine.requests.post")
    def test_runaway_translation_stream_stops_and_closes_response(self, post):
        import json
        response = FakeResponse([
            json.dumps({"message": {"content": "x" * 513}}),
            '{"message":{"content":"should not be read"},"done":true}',
        ])
        post.return_value = response
        with self.assertRaisesRegex(RuntimeError, "远超原文长度"):
            main._read_ollama_content({}, {
                "ollama_url": "http://localhost", "_translation_output_chars": 512})
        self.assertTrue(response.closed)

    @patch("translator.engine.requests.post")
    def test_stream_activity_reports_generated_and_thinking_characters(self, post):
        from unittest.mock import Mock
        activity = Mock()
        cfg = {"ollama_url": "http://localhost:11434", "_stream_activity": activity}
        post.return_value = FakeResponse([
            b'{"message":{"thinking":"abc"}}',
            b'{"message":{"content":"result"},"done":true}'])
        self.assertEqual(main._read_ollama_content({}, cfg), "result")
        self.assertEqual([call.args for call in activity.call_args_list], [(0, 3), (6, 3)])

    def setUp(self):
        self.cfg = {
            "ollama_url": "http://127.0.0.1:11434/",
            "request_timeout_seconds": 600,
        }

    @patch("translator.engine.requests.post")
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

    @patch("translator.engine.requests.post")
    def test_reports_streamed_ollama_error_and_closes_response(self, post):
        response = FakeResponse([b'{"error":"model runner stopped"}'])
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "model runner stopped"):
            main.ollama_chat_content({"model": "test"}, self.cfg)

        self.assertTrue(response.closed)


class TranslationBatchTests(unittest.TestCase):
    @patch("translator.engine.ollama_chat_content")
    def test_runaway_short_title_retries_once_without_context(self, chat):
        chat.side_effect = ["x" * 513, "基本常识"]
        self.assertEqual(main.translate_chunk(["基本的なことを学ぶ"], self.cfg, "previous"),
                         ["基本常識"])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args.args[0]["options"]["num_predict"], 256)
        self.assertNotIn("previous_translation", chat.call_args.args[0]["messages"][0]["content"])

    @patch("translator.engine.ollama_chat_content")
    def test_copied_context_is_retried_without_previous_translation(self, chat):
        context = "「喂，哥哥，你没事吧？」"
        chat.side_effect = [context + "\n\n他看着我。", "他看着我。"]
        self.assertEqual(main.translate_chunk(["彼が私を見ている。"], self.cfg, context), ["他看着我。"])
        self.assertEqual(chat.call_count, 2)
        self.assertNotIn(context, chat.call_args.args[0]["messages"][0]["content"])

    @patch("translator.engine.ollama_chat_content")
    def test_different_sources_with_duplicate_translation_are_retried(self, chat):
        chat.side_effect = ["这是错误的重复译文内容。\n\n这是错误的重复译文内容。", "早安。", "我们走吧。"]
        self.assertEqual(main.translate_chunk(["おはよう。", "移動するぞ。"], self.cfg),
                         ["早安。", "我們走吧。"])

    @patch("translator.engine.ollama_chat_content", return_value="你好。")
    def test_translation_disables_model_thinking(self, chat):
        self.assertEqual(main.translate_chunk(["こんにちは。"], self.cfg), ["你好。"])
        self.assertIs(chat.call_args.args[0]["think"], False)

    def setUp(self):
        self.cfg = {
            "ollama_url": "http://127.0.0.1:11434",
            "model": "test",
            "target_language": "繁體中文",
        }

    def test_make_batches_limits_paragraph_count(self):
        batches = main.make_batches(["短句"] * 81, limit=2200, max_items=40)

        self.assertEqual([len(batch) for batch in batches], [40, 40, 1])

    @patch("translator.engine.ollama_chat_content")
    def test_incomplete_translation_is_retried_in_smaller_batches(self, chat):
        chat.side_effect = [
            '譯一\n\n譯二\n\n譯三',
            '譯一\n\n譯二',
            '譯三\n\n譯四',
        ]

        result = main.translate_chunk(["一", "二", "三", "四"], self.cfg)

        self.assertEqual(result, ["譯一", "譯二", "譯三", "譯四"])
        self.assertEqual(chat.call_count, 3)

    @patch("translator.engine.ollama_chat_content")
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

    @patch("translator.engine.ollama_chat_content")
    def test_token_repeat_error_retries_single_paragraph_once(self, chat):
        chat.side_effect = [
            RuntimeError("Ollama 生成失敗：prediction aborted, token repeat limit reached"),
            '譯一',
        ]

        self.assertEqual(main.translate_chunk(["一"], self.cfg, "先前譯文"), ["譯一"])
        self.assertEqual(chat.call_count, 2)
        self.assertGreater(chat.call_args.args[0]["options"]["temperature"], 0.2)
        self.assertNotIn("先前譯文", chat.call_args.args[0]["messages"][0]["content"])

    @patch("translator.engine.ollama_chat_content")
    def test_repeated_single_paragraph_error_is_not_retried_forever(self, chat):
        chat.side_effect = RuntimeError("Ollama 生成失敗：prediction aborted, token repeat limit reached")

        with self.assertRaisesRegex(RuntimeError, "token repeat limit reached"):
            main.translate_chunk(["一"], self.cfg)

        self.assertEqual(chat.call_count, 2)

    @patch("translator.engine.ollama_chat_content")
    def test_other_ollama_error_is_not_retried(self, chat):
        chat.side_effect = RuntimeError("Ollama 生成失敗：model not found")

        with self.assertRaisesRegex(RuntimeError, "model not found"):
            main.translate_chunk(["一", "二"], self.cfg)

        self.assertEqual(chat.call_count, 1)




class ModelCheckTests(unittest.TestCase):
    @patch("translator.engine.installed_models", return_value={"hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"})
    @patch("builtins.input", side_effect=RuntimeError("lost sys.stdin"))
    def test_gui_missing_model_reports_available_name_without_prompt(self, prompt, _models):
        cfg = {"model": "aya-expanse-8b-GGUF:Q5_K_M"}

        with self.assertRaisesRegex(RuntimeError, "hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"):
            main.ensure_model(cfg, interactive=False)

        prompt.assert_not_called()

    @patch("translator.engine.installed_models", return_value={"hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"})
    @patch("builtins.input", side_effect=RuntimeError("lost sys.stdin"))
    def test_gui_installed_model_never_prompts(self, prompt, _models):
        cfg = {"model": "hf.co/bartowski/aya-expanse-8b-GGUF:Q5_K_M"}

        self.assertTrue(main.ensure_model(cfg, interactive=False))
        prompt.assert_not_called()


class TranslationBookTests(unittest.TestCase):
    def test_bilingual_export_preserves_original_and_reuses_translation(self):
        import zipfile
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.epub"
            source.write_bytes(b"source")
            original = main.SourceChapter(url="one", title="章", paragraphs=["學校", "原文二"],
                blocks=[{"type": "text", "text": "學校"},
                        {"type": "image", "url": "image"},
                        {"type": "text", "text": "原文二"}])
            translated = main._translated_chapter(original, ["學校譯文", "譯文二"])
            book = main.TranslationBook(source, folder, {"title": "小說"}, "简体中文")
            book.save(1, original, translated)
            bilingual = main.TranslationBook(source, folder, {"title": "小說"},
                                             "简体中文", bilingual=True)
            self.assertTrue(bilingual.completed(1, original))
            chapter = bilingual._chapters(1, 1)[0]
            self.assertEqual([b.get("text", b.get("url")) for b in chapter.blocks],
                             ["學校", "學校譯文", "image", "原文二", "譯文二"])
            output = bilingual.finish(1)
            self.assertIn("_双语对照", output.stem)
            self.assertNotEqual(output, book.output)
            with zipfile.ZipFile(output) as archive:
                text = "\n".join(archive.read(name).decode("utf-8")
                                 for name in archive.namelist() if name.endswith(".xhtml"))
            self.assertIn('class="original">學校', text)
            self.assertIn('class="translation">学校译文', text)
            self.assertLess(text.index('class="original">學校'),
                            text.index('class="translation">学校译文'))
            self.assertEqual(book.state["chapters"][0]["paragraphs"], ["學校譯文", "譯文二"])

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

            with patch("translator.storage.translation_book.translated_source_epub", side_effect=write_epub):
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
            with patch("translator.engine.select_epub_file", return_value=source), \
                 patch("translator.engine.inspect_epub", return_value={"title": "小說"}), \
                 patch("translator.engine.save_last_project"), \
                 patch("translator.engine.create_project_from_epub") as create:
                self.assertEqual(main.import_epub({"output_dir": str(root)}), work_dir)
            create.assert_not_called()
            self.assertEqual(main.load_json(work_dir / "project.json", {}), project)


if __name__ == "__main__":
    unittest.main()
