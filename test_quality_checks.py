import unittest

from quality_checks import check_translation_quality


class QualityCheckTests(unittest.TestCase):
    def test_flags_clear_review_cases_without_rejecting_short_dialogue(self):
        source = ["竜がいる", "彼は長い話をしていた。" * 4, "はい。"]
        target = ["竜がいる", "短い", "Yes."]
        warnings = check_translation_quality(source, target)
        self.assertEqual([(w.paragraph_index, w.category) for w in warnings],
                         [(1, "原文未变化"), (2, "译文可能过短")])

    def test_rejects_unaligned_paragraphs(self):
        with self.assertRaises(ValueError):
            check_translation_quality(["一", "二"], ["one"])


if __name__ == "__main__":
    unittest.main()
