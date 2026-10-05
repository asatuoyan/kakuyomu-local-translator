import unittest
from adaptive_batches import AdaptiveBatcher


class AdaptiveBatchTests(unittest.TestCase):
    def test_murasaki_keeps_short_chapter_whole_within_context_and_respects_degradation(self):
        paragraphs = ["原" * 100] * 14
        batcher = AdaptiveBatcher({"model": "murasaki-8b:latest", "target_language": "简体中文"}, 16384)
        self.assertEqual(batcher.take(paragraphs), paragraphs)
        batcher.context_length = 4096
        self.assertLess(len(batcher.take(paragraphs)), len(paragraphs))
        batcher.context_length = 16384
        batcher.mark_degraded()
        batcher.observe(1)
        self.assertLess(len(batcher.take(paragraphs)), len(paragraphs))

    def test_hy_mt_prefers_whole_chapter_if_budget_allows(self):
        paragraphs = ["原" * 100] * 15
        cfg = {"model": "hy-mt2-30b:latest"}
        batcher = AdaptiveBatcher(cfg, 8192)
        self.assertEqual(batcher.take(paragraphs, reference_chars=256), paragraphs)
        cfg["hy_mt_prefer_whole_chapter"] = False
        self.assertEqual(len(batcher.take(paragraphs)), 10)
        cfg["hy_mt_prefer_whole_chapter"] = True
        batcher.context_length = 4096
        self.assertLess(len(batcher.take(paragraphs, reference_chars=256)), 15)
    def test_hy_mt_window_and_generation_limits(self):
        paragraphs = ["原" * 32] * 100
        batcher = AdaptiveBatcher({"model": "hy-mt2-30b:latest"}, 4096)
        self.assertEqual(sum(map(len, batcher.take(paragraphs, reference_chars=256))), 512)
        batcher.items = 40
        self.assertEqual(sum(map(len, batcher.take(paragraphs, reference_chars=256))), 768)
        batcher.context_length = 262144
        batcher.chars = 2200
        self.assertLessEqual(sum(map(len, batcher.take(["原" * 100] * 40))), 1920)
    def test_model_presets_apply_before_first_request(self):
        for model, chars, items, ceiling in (
            ("hy-mt2-30b:latest", 1024, 16, 2200),
            ("hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", 800, 12, 2200),
            ("hy-mt2-1.8b:latest", 512, 8, 1536),
            ("murasaki-8b-q6:latest", 1024, 16, 1536),
            ("murasaki-14b-q6:latest", 1024, 16, 1536),
            ("unknown:latest", 800, 8, 2200),
        ):
            with self.subTest(model=model):
                batcher = AdaptiveBatcher({"model": model}, 16384)
                self.assertEqual((batcher.chars, batcher.items, batcher.ceiling),
                                 (chars, items, ceiling))

    def test_preset_is_capped_by_user_limits_and_runtime_window(self):
        batcher = AdaptiveBatcher({"model": "hy-mt2-30b:latest",
                                  "translation_chunk_chars": 500,
                                  "translation_chunk_paragraphs": 3}, 2048)
        self.assertEqual((batcher.chars, batcher.items), (500, 3))
        self.assertLessEqual(sum(map(len, batcher.take(["原" * 100] * 20))), 300)

    def test_grows_after_clean_requests_and_shrinks_after_fallback(self):
        batcher = AdaptiveBatcher({}, 16384)
        paragraphs = ["原" * 100] * 100
        self.assertEqual(len(batcher.take(paragraphs)), 8)
        batcher.observe(10)
        batcher.take(paragraphs)
        batcher.observe(10)
        self.assertEqual(len(batcher.take(paragraphs)), 10)
        batcher.mark_degraded()
        batcher.observe(10)
        self.assertEqual(len(batcher.take(paragraphs)), 5)

    def test_reserves_context_and_obeys_configured_limits(self):
        batcher = AdaptiveBatcher({"translation_chunk_chars": 400,
                                  "translation_chunk_paragraphs": 2}, 2048)
        self.assertLessEqual(len(batcher.take(["原" * 100] * 20)), 2)
        self.assertEqual(len(batcher.take(["原" * 100] * 20, reference_chars=600)), 1)
        for _ in range(20):
            batcher.observe(1)
        self.assertLessEqual(batcher.chars, 400)
        self.assertLessEqual(batcher.items, 2)

    def test_slow_batch_shrinks_and_long_single_paragraph_is_preserved(self):
        batcher = AdaptiveBatcher({}, 4096)
        long = "原" * 5000
        self.assertEqual(batcher.take([long, "下一段"]), [long])
        batcher.observe(60)
        self.assertEqual(batcher.chars, 400)
        self.assertEqual(batcher.items, 4)
