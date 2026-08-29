from __future__ import annotations

import unittest

from scripts import score_testset as scorer


class NormalizeTextTests(unittest.TestCase):
    def test_strip_and_collapse_whitespace(self) -> None:
        self.assertEqual(scorer.normalize_text("  材料 A \n 材料 B\t\n"), "材料 a 材料 b")

    def test_fullwidth_chars_normalized_to_halfwidth(self) -> None:
        self.assertEqual(scorer.normalize_text("ＡＢＣ１２３"), "abc123")

    def test_fullwidth_and_ideographic_punctuation_unified(self) -> None:
        self.assertEqual(scorer.normalize_text("材料：Ａ，Ｂ。；"), scorer.normalize_text("材料:A,B.;"))

    def test_quotes_and_brackets_unified(self) -> None:
        self.assertEqual(scorer.normalize_text("“章程”（必要）"), scorer.normalize_text('"章程"(必要)'))

    def test_empty_and_none(self) -> None:
        self.assertEqual(scorer.normalize_text(""), "")
        self.assertEqual(scorer.normalize_text(None), "")
        self.assertEqual(scorer.normalize_text("  \n\t "), "")


class TokenizeTests(unittest.TestCase):
    def test_ascii_run_is_single_token_and_cjk_by_char(self) -> None:
        self.assertEqual(
            scorer.tokenize(scorer.normalize_text("EXCEL表格式xlsx")),
            ["excel", "表", "格", "式", "xlsx"],
        )

    def test_punctuation_dropped(self) -> None:
        self.assertEqual(scorer.tokenize("a，b。c；"), ["a", "b", "c"])


class ExactMatchTests(unittest.TestCase):
    def test_match_after_normalization(self) -> None:
        self.assertEqual(scorer.exact_match("材料：Ａ，Ｂ", "材料:A,B"), 1)

    def test_mismatch(self) -> None:
        self.assertEqual(scorer.exact_match("材料A", "材料B"), 0)


class CharF1Tests(unittest.TestCase):
    def test_perfect_match_is_one(self) -> None:
        tokens = scorer.tokenize(scorer.normalize_text("申请材料：身份证，户口簿"))
        self.assertEqual(scorer.char_f1(tokens, list(tokens)), 1.0)

    def test_partial_overlap_known_value(self) -> None:
        pred = ["申", "请", "材", "料", "表"]
        gold = ["申", "请", "材", "料"]
        # p=4/5, r=4/4, F1=2*p*r/(p+r)=8/9
        self.assertAlmostEqual(scorer.char_f1(pred, gold), 8 / 9, places=6)

    def test_no_overlap_is_zero(self) -> None:
        self.assertEqual(scorer.char_f1(["甲"], ["乙"]), 0.0)

    def test_empty_vs_nonempty_is_zero_and_both_empty_is_one(self) -> None:
        self.assertEqual(scorer.char_f1([], ["甲"]), 0.0)
        self.assertEqual(scorer.char_f1(["甲"], []), 0.0)
        self.assertEqual(scorer.char_f1([], []), 1.0)

    def test_answer_f1_wrapper_normalizes(self) -> None:
        self.assertEqual(scorer.answer_f1("材料：Ａ，Ｂ", "材料:A,B"), 1.0)


class ScoreRowsTests(unittest.TestCase):
    def test_unanswered_refused_and_correct_scoring(self) -> None:
        rows = [
            {"test_id": "X1", "dataset": "pilot", "question": "q", "expected_answer": "答案", "answer_type": "materials"},
            {"test_id": "X2", "dataset": "pilot", "question": "q", "expected_answer": "答案", "answer_type": "process"},
            {"test_id": "X3", "dataset": "pilot", "question": "q", "expected_answer": "答案", "answer_type": "fee"},
        ]
        items = scorer.score_rows(rows, {"X1": "答案", "X2": "   "})  # X3 未答，X2 拒答
        by_id = {item["test_id"]: item for item in items}
        self.assertEqual(by_id["X1"]["em"], 1)
        self.assertEqual(by_id["X1"]["f1"], 1.0)
        self.assertTrue(by_id["X2"]["refused"])
        self.assertEqual(by_id["X2"]["em"], 0)
        self.assertFalse(by_id["X3"]["answered"])
        self.assertFalse(by_id["X3"]["refused"])
        summary = scorer.group_summary(items)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["em_hits"], 1)
        self.assertAlmostEqual(summary["em"], 1 / 3, places=6)
        self.assertEqual((summary["answered"], summary["refused"], summary["unanswered"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
