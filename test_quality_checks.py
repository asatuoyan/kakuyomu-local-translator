import unittest

from quality_checks import check_translation_quality


class QualityCheckTests(unittest.TestCase):
    def test_warns_about_context_copied_into_another_paragraph(self):
        warnings = check_translation_quality(
            ["おい、兄さん大丈夫かい？", "ぼやける視界の中、老人が私を見る。", "移動するぞ？"],
            ["「喂，哥哥，你没事吧？」", "「喂，哥哥，你没事吧？」\n\n视野模糊。",
             "「喂，哥哥，你没事吧？」\n「喂，哥哥，你没事吧？」"])
        self.assertEqual([w.paragraph_index for w in warnings], [2, 3])

    def test_identical_source_dialogue_is_not_flagged_as_context_copy(self):
        self.assertEqual(check_translation_quality(["同じ台詞", "同じ台詞"],
                         ["相同的台词再次出现了。", "相同的台词再次出现了。"]), [])

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
