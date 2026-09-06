#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/compare_thinking_ab.py 单测（不连真实 LLM / 不读大文件）。

覆盖：
  1. 归一化匹配：同义标点（全角/半角）/ 空白折叠 / NFKC 数字视为相等，
     多重集语义（重复命题计数）与仅 A / 仅 B 归类；
  2. 块级对齐统计（两边都有 / 仅 A / 仅 B / 并集）；
  3. statement 长度分位（nearest-rank）；
  4. summary 指标提取（parse_error/failed/抽样块数/思考链/max_tokens）与缺失 N/A；
  5. "两边都空"估算的条件（抽样块数一致且无失败块）；
  6. 差异块抽样排序（差异命题数降序、chunk_id 升序、limit 截断）；
  7. 原文摘录：流式扫描、空白折叠、长度截断、未命中标注；
  8. CSV 缺列容错：缺 chunk_id 报错退出；缺 statement/predicate 标 N/A 不炸；
  9. 报告渲染含全部关键小节标题与关键数字；
 10. main() 端到端（临时小 CSV + mini summary + chunks）。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import compare_thinking_ab as cab  # noqa: E402
from scripts.score_testset import normalize_text  # noqa: E402 —— 验证归一化来源


# ---------------------------------------------------------------- 桩件

FULL_COLUMNS = ["chunk_id", "service_id", "doc_id", "source_field", "predicate",
                "condition_type", "object_value", "statement"]


def _prop(chunk_id: str, statement: str, predicate: str = "其他",
          source_field: str = "other") -> dict:
    return {"chunk_id": chunk_id, "statement": statement,
            "predicate": predicate, "source_field": source_field}


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    columns = FULL_COLUMNS if columns is None else columns
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _mini_summary(path: Path, *, sampled: int = 3, ok: int = 2, empty: int = 1,
                  parse_error: int = 0, failed: int = 0, thinking: str = "开",
                  max_tokens: int = 16000) -> None:
    """按 extract_propositions_pilot.write_summary 的行格式写 mini summary。"""
    path.write_text("\n".join([
        "# 命题抽取试点报告（SynthKG + SocraticKG 路线）",
        "",
        "- 生成时间：2026-09-06 10:00:00",
        f"- 模型：qwen3.8-27b（endpoint http://fake/v1/chat/completions，"
        f"temperature 0.1，max_tokens {max_tokens}，思考链：{thinking}）",
        f"- prompt 版本：pilot-v1",
        f"- 参数：--limit 3 --seed 11 --max-tokens {max_tokens}",
        "",
        "## 块统计",
        "",
        f"- 抽样块数：{sampled}（扫描 100 行，过滤短块 0，5 个分层，候选 8）",
        f"- 本次运行：{sampled} 块；断点跳过 0 块（state 累计完成 {sampled} 块）",
        f"- 成功（有命题）：{ok} 块",
        f"- 空抽（无有效事实，输出 []）：{empty} 块",
        f"- 解析失败（parse_error）：{parse_error} 块",
        f"- 调用失败（failed，重试后仍失败）：{failed} 块",
        "",
        "## 谓词分布",
        "",
        "| 谓词 | 数量 | 占比 |",
        "|---|---|---|",
    ]), encoding="utf-8")


# ================================================================ 归一化匹配


class TestNormalizeMatching(unittest.TestCase):

    def test_normalize_reused_from_score_testset(self):
        # 归一化函数必须与 score_testset 的实现同源（NFKC + 标点映射 + 小写 + 折叠）
        self.assertIs(cab.normalize_text, normalize_text)
        self.assertEqual(cab.normalize_text("Ａｂｃ１２３"), "abc123")

    def test_punctuation_and_whitespace_treated_equal(self):
        # 全角句号 vs 半角句点、全角数字 NFKC → 视为同一命题
        # （注意：normalize_text 折叠空白但不去除词间空格，故此处不引入空格差异）
        a = [_prop("c1", "测试事项的办理时限为２０个工作日。", "办理时限")]
        b = [_prop("c1", "测试事项的办理时限为20个工作日.", "办理时限")]
        result = cab.match_statements(a, b)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["only_a"], [])
        self.assertEqual(result["only_b"], [])

    def test_whitespace_folding_treated_equal(self):
        # 连续空白（含换行/制表）折叠为单空格后视为同一命题
        a = [_prop("c1", "事项 的\n办理时限\t为20个工作日.")]
        b = [_prop("c1", "事项  的  办理时限  为20个工作日.")]
        self.assertEqual(cab.match_statements(a, b)["matched"], 1)

    def test_multiset_semantics_for_duplicates(self):
        # A 有两条归一化相同的命题，B 只有一条 → 匹配 1、仅 A 1（多重集）
        a = [_prop("c1", "同一命题。"), _prop("c1", "同一命题。")]
        b = [_prop("c1", "同一命题。")]
        result = cab.match_statements(a, b)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(result["only_a"]), 1)
        self.assertEqual(result["only_b"], [])

    def test_only_a_only_b_classification_keeps_fields(self):
        a = [_prop("c1", "两边都有的命题。", "办理时限"),
             _prop("c1", "只有A组输出的命题。", "材料要求")]
        b = [_prop("c1", "两边都有的命题。", "办理时限"),
             _prop("c1", "只有B组输出的命题。", "禁止情形")]
        result = cab.match_statements(a, b)
        self.assertEqual(result["matched"], 1)
        self.assertEqual([p["statement"] for p in result["only_a"]],
                         ["只有A组输出的命题。"])
        self.assertEqual([p["predicate"] for p in result["only_a"]], ["材料要求"])
        self.assertEqual([p["statement"] for p in result["only_b"]],
                         ["只有B组输出的命题。"])
        self.assertEqual([p["predicate"] for p in result["only_b"]], ["禁止情形"])

    def test_empty_statements_all_match_each_other(self):
        # 归一化后都为空串的 statement 也参与多重集匹配（不特殊处理）
        a = [_prop("c1", "")]
        b = [_prop("c1", "  ")]
        self.assertEqual(cab.match_statements(a, b)["matched"], 1)


# ================================================================ 块级对齐


class TestBlockAlignment(unittest.TestCase):

    def test_align_blocks_counts(self):
        a_by = {"c1": [], "c2": [], "c4": []}
        b_by = {"c2": [], "c3": [], "c4": []}
        align = cab.align_blocks(a_by, b_by)
        self.assertEqual(align["both"], 2)      # c2, c4
        self.assertEqual(align["only_a"], 1)    # c1
        self.assertEqual(align["only_b"], 1)    # c3
        self.assertEqual(align["union"], 4)

    def test_group_by_chunk_preserves_order(self):
        rows = [_prop("c2", "甲"), _prop("c1", "乙"), _prop("c2", "丙")]
        grouped = cab.group_by_chunk(rows)
        self.assertEqual(list(grouped), ["c2", "c1"])  # 首次出现顺序
        self.assertEqual([r["statement"] for r in grouped["c2"]], ["甲", "丙"])


# ================================================================ 分位


class TestPercentiles(unittest.TestCase):

    def test_nearest_rank_values(self):
        result = cab.percentile_nearest_rank([40, 10, 30, 20])
        self.assertEqual(result, {"p25": 10, "p50": 20, "p75": 30})

    def test_single_value(self):
        self.assertEqual(cab.percentile_nearest_rank([7]),
                         {"p25": 7, "p50": 7, "p75": 7})

    def test_empty_returns_none(self):
        self.assertIsNone(cab.percentile_nearest_rank([]))


# ================================================================ summary 指标


class TestSummaryMetrics(unittest.TestCase):

    def test_parse_full_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            _mini_summary(path, sampled=1000, parse_error=2, failed=1,
                          thinking="开", max_tokens=16000)
            metrics = cab.parse_summary_metrics(path)
            self.assertEqual(metrics["sampled_blocks"], 1000)
            self.assertEqual(metrics["ok_blocks"], 2)
            self.assertEqual(metrics["empty_blocks"], 1)
            self.assertEqual(metrics["parse_error"], 2)
            self.assertEqual(metrics["failed"], 1)
            self.assertEqual(metrics["thinking"], "开")
            self.assertEqual(metrics["max_tokens"], 16000)

    def test_missing_file_all_none(self):
        metrics = cab.parse_summary_metrics(Path(tempfile.gettempdir()) / "no.md")
        self.assertTrue(all(v is None for v in metrics.values()))

    def test_none_path_all_none(self):
        self.assertTrue(all(v is None for v in cab.parse_summary_metrics(None).values()))


# ================================================================ 两边都空估算


class TestEstimateBothEmpty(unittest.TestCase):

    def test_estimate_when_consistent_and_clean(self):
        a = {"sampled_blocks": 10, "parse_error": 0, "failed": 0}
        b = {"sampled_blocks": 10, "parse_error": 0, "failed": 0}
        value, note = cab.estimate_both_empty(a, b, union_blocks=7)
        self.assertEqual(value, 3)
        self.assertIn("10", note)

    def test_none_when_failures_exist(self):
        a = {"sampled_blocks": 10, "parse_error": 1, "failed": 0}
        b = {"sampled_blocks": 10, "parse_error": 0, "failed": 0}
        self.assertIsNone(cab.estimate_both_empty(a, b, 7)[0])

    def test_none_when_sampled_mismatch(self):
        a = {"sampled_blocks": 10, "parse_error": 0, "failed": 0}
        b = {"sampled_blocks": 8, "parse_error": 0, "failed": 0}
        self.assertIsNone(cab.estimate_both_empty(a, b, 7)[0])

    def test_none_when_summary_missing(self):
        a = {"sampled_blocks": None, "parse_error": None, "failed": None}
        b = {"sampled_blocks": None, "parse_error": None, "failed": None}
        value, note = cab.estimate_both_empty(a, b, 7)
        self.assertIsNone(value)
        self.assertIn("无法估算", note)


# ================================================================ 差异块与原文摘录


class TestDiffChunksAndExcerpts(unittest.TestCase):

    def _per_block(self) -> dict:
        return {
            "c1": {"matched": 5, "only_a": [], "only_b": []},
            "c2": {"matched": 1, "only_a": [_prop("c2", "a")], "only_b": []},
            "c3": {"matched": 0, "only_a": [_prop("c3", "a1"), _prop("c3", "a2")],
                   "only_b": [_prop("c3", "b1")]},
            "c4": {"matched": 2, "only_a": [], "only_b": [_prop("c4", "b1"),
                                                          _prop("c4", "b2")]},
        }

    def test_pick_diff_chunks_orders_by_diff_size(self):
        picked = cab.pick_diff_chunks(self._per_block(), 2)
        self.assertEqual([cid for cid, _ in picked], ["c3", "c4"])
        # c3 与 c4 差异数同为 3 → chunk_id 升序；c1 无差异不入选；limit=2 截断

    def test_pick_diff_chunks_zero_limit(self):
        self.assertEqual(cab.pick_diff_chunks(self._per_block(), 0), [])

    def test_fetch_chunk_excerpts_collapses_and_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            chunks = tmp / "documents_chunks.csv"
            with chunks.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["chunk_id", "content"])
                writer.writerow(["c1", "第一行\n\n第二行\t有制表  符"])
                writer.writerow(["c9", "不需要的块"])
                writer.writerow(["c2", "目标块原文"])
                writer.writerow(["c2", "重复块（不应覆盖）"])
            excerpts = cab.fetch_chunk_excerpts(chunks, ["c2", "missing"], chars=10)
            self.assertEqual(excerpts.get("c2"), "目标块原文")
            self.assertNotIn("missing", excerpts)
            self.assertNotIn("c1", excerpts)
            # 空白折叠：多行文本压成单空格
            folded = cab.fetch_chunk_excerpts(chunks, ["c1"])
            self.assertEqual(folded["c1"], "第一行 第二行 有制表 符")

    def test_fetch_no_chunks_path_returns_empty(self):
        self.assertEqual(cab.fetch_chunk_excerpts(None, ["c1"]), {})

    def test_fetch_missing_chunks_file_returns_empty(self):
        excerpts = cab.fetch_chunk_excerpts(
            Path(tempfile.gettempdir()) / "no-such-chunks.csv", ["c1"])
        self.assertEqual(excerpts, {})


# ================================================================ CSV 读取容错


class TestLoadProps(unittest.TestCase):

    def test_missing_chunk_id_column_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            _write_csv(path, [{"statement": "无块标识"}],
                       columns=["statement", "predicate"])
            with self.assertRaises(cab.CompareError):
                cab.load_props(path)

    def test_missing_file_raises(self):
        with self.assertRaises(cab.CompareError):
            cab.load_props(Path(tempfile.gettempdir()) / "no-such-props.csv")

    def test_empty_rows_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.csv"
            _write_csv(path, [], columns=["chunk_id", "statement"])
            with self.assertRaises(cab.CompareError):
                cab.load_props(path)

    def test_missing_optional_columns_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.csv"
            _write_csv(path, [{"chunk_id": "c1"}], columns=["chunk_id"])
            group = cab.load_props(path)
            self.assertEqual(group["missing_columns"],
                             ["statement", "predicate", "source_field"])
            self.assertEqual(group["rows"][0]["statement"], "")
            self.assertEqual(group["rows"][0]["predicate"], "")

    def test_skips_rows_with_empty_chunk_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.csv"
            _write_csv(path, [{"chunk_id": "c1", "statement": "正常"},
                              {"chunk_id": "", "statement": "无 id"},
                              {"chunk_id": "  ", "statement": "空白 id"}],
                       columns=["chunk_id", "statement"])
            group = cab.load_props(path)
            self.assertEqual(len(group["rows"]), 1)
            self.assertEqual(group["skipped_no_id"], 2)


# ================================================================ 报告渲染


class TestRenderReport(unittest.TestCase):

    def _fixture(self, tmp: Path):
        a_rows = [
            # c1 第 1 条：句号 vs 句点（标点差异）→ 归一化后匹配
            _prop("c1", "测试事项的办理时限为20个工作日。", "办理时限"),
            _prop("c1", "仅A组输出的补充命题。", "材料要求"),
            _prop("c2", "共同命题，两组完全一致。", "其他"),
            _prop("c3", "仅A块独有的命题。", "其他"),
        ]
        b_rows = [
            _prop("c1", "测试事项的办理时限为20个工作日.", "办理时限"),
            _prop("c2", "共同命题，两组完全一致。", "其他"),
            _prop("c4", "仅B块独有的命题。", "禁止情形"),
        ]
        _write_csv(tmp / "a.csv", a_rows)
        _write_csv(tmp / "b.csv", b_rows)
        a_summary = tmp / "a_summary.md"
        b_summary = tmp / "b_summary.md"
        _mini_summary(a_summary, sampled=4, thinking="开", max_tokens=16000)
        _mini_summary(b_summary, sampled=4, thinking="关", max_tokens=6000)
        group_a = cab.load_props(tmp / "a.csv")
        group_b = cab.load_props(tmp / "b.csv")
        return group_a, group_b, a_summary, b_summary

    def test_report_contains_all_section_titles_and_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            group_a, group_b, a_summary, b_summary = self._fixture(tmp)
            report = cab.render_report(
                label_a="开思考(A)", label_b="关思考(B)",
                path_a="a.csv", path_b="b.csv",
                path_a_summary=str(a_summary), path_b_summary=str(b_summary),
                group_a=group_a, group_b=group_b,
                a_metrics=cab.parse_summary_metrics(a_summary),
                b_metrics=cab.parse_summary_metrics(b_summary),
                sample_chunks=15)
            for title in ("# 思考链开/关 A/B 质量对比报告", "## 块级对齐",
                          "## 密度对比", "## 谓词分布对比", "## 稳定性",
                          "## 同块命题对齐（语句级匹配）", "## 差异块抽样",
                          "## 小结"):
                self.assertIn(title, report, msg=title)
            # 块级对齐：共同 c1/c2=2，仅 A c3=1，仅 B c4=1
            self.assertIn("| 两边都有命题 | 2 |", report)
            self.assertIn("| 仅 开思考(A) | 1 |", report)
            self.assertIn("| 仅 关思考(B) | 1 |", report)
            # 两边都空：抽样 4 - 出现过命题的块 4 = 0（估算）
            self.assertIn("| 两边都空 | 0 |", report)
            # 命题总数与共同块密度
            self.assertIn("| 命题总数（CSV 全部行） | 4 | 3 |", report)
            self.assertIn("| 共同块上命题数 | 3 | 2 |", report)
            # 语句级匹配：matched=2，仅 A=1，仅 B=0（c1 的标点差异不视为不同）
            self.assertIn("| 完全匹配（A∩B） | 2 |", report)
            self.assertIn("| 仅 开思考(A) | 1 |", report)
            # 稳定性 N/A 与数字并存（本 fixture summary 均给出 0）
            self.assertIn("parse_error 块数（summary）", report)
            self.assertIn("0 块", report)
            # 差异块：c1（1 条仅 A）、c3/c4 是非共同块不参与匹配
            self.assertIn("### 1. c1", report)
            self.assertIn("仅A组输出的补充命题。", report)
            self.assertIn("N/A（未提供 --chunks 或块未命中）", report)
            # 小结关键数字
            self.assertIn("共同块数（两边都有命题）：2", report)
            self.assertIn("仅 A / 仅 B 命题数：1 / 0", report)

    def test_report_na_when_statement_column_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # A 缺 predicate 列、B 缺 statement 列：两组对应统计均标 N/A 而不崩
            _write_csv(tmp / "a.csv", [{"chunk_id": "c1", "statement": "有语句"}],
                       columns=["chunk_id", "statement"])
            _write_csv(tmp / "b.csv", [{"chunk_id": "c1", "predicate": "其他"}],
                       columns=["chunk_id", "predicate"])
            group_a = cab.load_props(tmp / "a.csv")
            group_b = cab.load_props(tmp / "b.csv")
            report = cab.render_report(
                label_a="A", label_b="B", path_a="a.csv", path_b="b.csv",
                path_a_summary=None, path_b_summary=None,
                group_a=group_a, group_b=group_b,
                a_metrics=cab.parse_summary_metrics(None),
                b_metrics=cab.parse_summary_metrics(None),
                sample_chunks=15)
            self.assertIn("N/A：某组 CSV 缺少 statement 列", report)
            self.assertIn("N/A（某组 CSV 缺少 predicate 列）", report)
            self.assertIn("（未提供 summary）", report)
            # summary 缺失时参数区与稳定性均为 N/A，不崩
            self.assertIn("思考链 N/A", report)

    def test_report_identical_sets_no_diff_chunks(self):
        rows = [_prop("c1", "完全一致的命题。")]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_csv(tmp / "a.csv", rows)
            _write_csv(tmp / "b.csv", [dict(rows[0])])
            report = cab.render_report(
                label_a="A", label_b="B", path_a="a.csv", path_b="b.csv",
                path_a_summary=None, path_b_summary=None,
                group_a=cab.load_props(tmp / "a.csv"),
                group_b=cab.load_props(tmp / "b.csv"),
                a_metrics={}, b_metrics={}, sample_chunks=15)
            self.assertIn("（共同块内没有差异命题：两组命题集合完全一致）", report)


# ================================================================ main 端到端


class TestMainEndToEnd(unittest.TestCase):

    def test_main_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a_rows = [
                _prop("c1", "测试事项的办理时限为20个工作日。", "办理时限"),
                _prop("c1", "仅A组输出的补充命题。", "材料要求"),
                _prop("c2", "共同命题。", "其他"),
            ]
            b_rows = [
                _prop("c1", "测试事项的办理时限为 20 个工作日.", "办理时限"),
                _prop("c2", "共同命题。", "其他"),
            ]
            _write_csv(tmp / "a.csv", a_rows)
            _write_csv(tmp / "b.csv", b_rows)
            a_summary, b_summary = tmp / "sa.md", tmp / "sb.md"
            _mini_summary(a_summary, sampled=2, thinking="开", max_tokens=16000)
            _mini_summary(b_summary, sampled=2, thinking="关", max_tokens=6000)
            chunks = tmp / "chunks.csv"
            with chunks.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["chunk_id", "content"])
                writer.writerow(["c1", "这里是 c1 的原文内容，含换行\n第二行"])
            report = tmp / "sub" / "report.md"
            rc = cab.main([
                "--a", str(tmp / "a.csv"), "--b", str(tmp / "b.csv"),
                "--a-summary", str(a_summary), "--b-summary", str(b_summary),
                "--chunks", str(chunks), "--report", str(report),
                "--sample-chunks", "5"])
            self.assertEqual(rc, 0)
            text = report.read_text(encoding="utf-8")
            self.assertIn("# 思考链开/关 A/B 质量对比报告", text)
            self.assertIn("## 小结", text)
            # 原文摘录来自 chunks（空白折叠）
            self.assertIn("这里是 c1 的原文内容，含换行 第二行", text)
            self.assertIn("仅A组输出的补充命题。", text)

    def test_main_missing_required_column_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_csv(tmp / "a.csv", [{"statement": "无块标识"}],
                       columns=["statement"])
            _write_csv(tmp / "b.csv", [{"chunk_id": "c1"}],
                       columns=["chunk_id", "statement"])
            rc = cab.main(["--a", str(tmp / "a.csv"), "--b", str(tmp / "b.csv"),
                           "--report", str(tmp / "r.md")])
            self.assertEqual(rc, 2)
            self.assertFalse((tmp / "r.md").exists())

    def test_main_invalid_args_exit(self):
        with self.assertRaises(SystemExit):
            cab.parse_args(["--sample-chunks", "-1"])


if __name__ == "__main__":
    unittest.main()
