#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""思考链开/关 A/B 抽取质量对比报告生成器。

输入：A 组命题 CSV（scripts/extract_propositions_pilot.py --enable-thinking 跑出）
     与 B 组命题 CSV（关思考跑出），各列同 propositions_pilot.csv：
       chunk_id, service_id, doc_id, source_field, predicate,
       condition_type, object_value, statement
     可选 --a-summary / --b-summary（各自 summary.md，用于提取 parse_error /
     failed 等块级数字；缺失或匹配不到时对应项标 N/A）。
输出：Markdown 对比报告（--report）。

比较维度：
  1. 块级对齐：按 chunk_id 对齐两组，统计两边都有命题 / 仅 A / 仅 B 的块数。
     "两边都空"无法从 CSV 直接得知（CSV 不记录空抽块），在两组 summary 的
     抽样块数一致且无 parse_error/failed 时给出估算并标注依据，否则标 N/A。
  2. 密度对比：命题总数、共同块上每块平均命题数、statement 字符数
     P25/P50/P75（nearest-rank 分位）。
  3. 谓词分布对比：并排两列 + 差值（A−B），按差值绝对值降序。
  4. 稳定性：parse_error / failed 块数（summary 缺失时 N/A）。
  5. 同块命题对齐：对共同块做语句级匹配——statement 归一化后（复用
     scripts/score_testset.py 的 normalize_text：NFKC + 中文标点映射 + ASCII
     小写 + 空白折叠）按多重集取交集：完全匹配数、仅 A、仅 B；按差异命题数
     抽样展示 N 个"差异块"（仅 A / 仅 B 命题并排 + 原文前 200 字），供人工
     判断哪版更优。原文来自 --chunks（可选；流式扫描、找齐即停，不整表载入）。
  6. 小结：摆齐关键数字，不替人下结论。

用法（仓库根目录）：
  python scripts/compare_thinking_ab.py \
      --a data/pilot/propositions_pilot_thinking_on.csv \
      --b data/pilot/propositions_pilot.csv \
      --a-summary data/pilot/propositions_pilot_thinking_on_summary.md \
      --b-summary data/pilot/propositions_pilot_summary.md \
      --chunks data/pilot/documents_chunks.csv \
      --report kg/import/reports/thinking_ab_compare.md

退出码：0 正常生成；2 输入错误（文件缺失 / 缺 chunk_id 列 / CSV 无有效行）。
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Sequence

# statement 归一化复用 scripts/score_testset.py 的 normalize_text
# （NFKC 全角→半角 + 中文标点映射 + ASCII 小写 + 空白折叠）。
# 单测从仓库根以包路径导入（scripts 是命名空间包）；直接运行本脚本时
# sys.path[0] 是 scripts/，退回平级导入。
try:
    from scripts.score_testset import normalize_text  # type: ignore
except ImportError:  # pragma: no cover —— 直接运行路径，测试走上一分支
    from score_testset import normalize_text  # type: ignore

#: 命题 CSV 必需列（缺 chunk_id 无法按块对齐 → 报错退出）
REQUIRED_COLUMNS = ("chunk_id",)
#: 可选列：缺失时对应统计标 N/A 并告警（不中断）
OPTIONAL_COLUMNS = ("statement", "predicate", "source_field")

#: summary.md 中可提取的块级指标（extract_propositions_pilot.write_summary 的行格式）
SUMMARY_PATTERNS: dict[str, str] = {
    "sampled_blocks": r"抽样块数：(\d+)",
    "ok_blocks": r"成功（有命题）：(\d+)\s*块",
    "empty_blocks": r"空抽（无有效事实，输出 \[\]）：(\d+)\s*块",
    "parse_error": r"解析失败（parse_error）：(\d+)\s*块",
    "failed": r"调用失败（failed，重试后仍失败）：(\d+)\s*块",
    "thinking": r"思考链：(开|关)",
    "max_tokens": r"max_tokens\s*(\d+)",
}
_INT_KEYS = {k for k in SUMMARY_PATTERNS if k != "thinking"}


class CompareError(Exception):
    """输入错误，应导致非 0 退出码。"""


def warn(message: str) -> None:
    print(f"警告: {message}", file=sys.stderr)


# ================================================================ 读取

def load_props(path: str | Path) -> dict:
    """读一组命题 CSV（utf-8-sig 容忍 BOM）。

    容错：chunk_id 列缺失或文件无有效行 → CompareError；statement / predicate /
    source_field 列缺失 → 不中断，记录到 missing_columns（对应统计标 N/A）；
    单行 chunk_id 为空 → 跳过并计数 skipped_no_id。

    返回 {"rows": [...], "missing_columns": [...], "skipped_no_id": int}，
    行只保留关心的键（缺列的键置空字符串）。
    """
    p = Path(path)
    if not p.is_file():
        raise CompareError(f"命题 CSV 不存在: {p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise CompareError(f"{p}: 缺少必需列 {missing}，实际列 {fieldnames}")
        optional_missing = [c for c in OPTIONAL_COLUMNS if c not in fieldnames]
        if optional_missing:
            warn(f"{p}: 缺少列 {optional_missing}，对应统计将标 N/A")
        rows: list[dict[str, str]] = []
        skipped_no_id = 0
        for record in reader:
            chunk_id = (record.get("chunk_id") or "").strip()
            if not chunk_id:
                skipped_no_id += 1
                continue
            rows.append({
                "chunk_id": chunk_id,
                "statement": (record.get("statement") or "").strip(),
                "predicate": (record.get("predicate") or "").strip(),
                "source_field": (record.get("source_field") or "").strip(),
            })
    if not rows:
        raise CompareError(f"{p}: 没有读到任何有效命题行（chunk_id 非空）")
    if skipped_no_id:
        warn(f"{p}: {skipped_no_id} 行 chunk_id 为空，已跳过")
    return {"rows": rows, "missing_columns": optional_missing,
            "skipped_no_id": skipped_no_id}


def group_by_chunk(rows: Sequence[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """按 chunk_id 归组（保持 CSV 内出现顺序）。"""
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["chunk_id"], []).append(row)
    return grouped


# ================================================================ 统计

def align_blocks(a_by_chunk: dict[str, list], b_by_chunk: dict[str, list]) -> dict:
    """块级对齐：两边都有命题 / 仅 A / 仅 B 的块数（CSV 只记录有命题的块）。"""
    a_ids, b_ids = set(a_by_chunk), set(b_by_chunk)
    return {
        "both": len(a_ids & b_ids),
        "only_a": len(a_ids - b_ids),
        "only_b": len(b_ids - a_ids),
        "union": len(a_ids | b_ids),
    }


def estimate_both_empty(a_metrics: dict, b_metrics: dict,
                        union_blocks: int) -> tuple[int | None, str]:
    """估算"两边都空"的块数（CSV 不记录空块，只能从 summary 推）。

    仅当两组 summary 都给出抽样块数且一致、且 parse_error/failed 均为 0 时：
    估算 = 抽样块数 - 出现过命题的块数（并集）。否则返回 (None, 原因)。
    """
    sa, sb = a_metrics.get("sampled_blocks"), b_metrics.get("sampled_blocks")
    if sa is None or sb is None:
        return None, "summary 缺少\"抽样块数\"，无法估算"
    if sa != sb:
        return None, f"两组抽样块数不一致（A={sa}，B={sb}），无法估算"
    if any((a_metrics.get(k) or 0) + (b_metrics.get(k) or 0)
           for k in ("parse_error", "failed")):
        return None, "存在 parse_error/failed 块（未完成块不写 CSV），无法估算"
    return sa - union_blocks, f"估算 = 抽样块数 {sa} - 出现过命题的块数 {union_blocks}"


def percentile_nearest_rank(values: Sequence[int],
                            percents: Sequence[int] = (25, 50, 75)) -> dict | None:
    """nearest-rank 分位数（P25/P50/P75）；空输入返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    out = {}
    for pct in percents:
        rank = max(1, math.ceil(pct / 100 * len(ordered)))
        out[f"p{pct}"] = ordered[min(rank, len(ordered)) - 1]
    return out


def predicate_counts(rows: Sequence[dict[str, str]],
                     missing_columns: Sequence[str]) -> Counter | None:
    """谓词分布；predicate 列缺失（在 missing_columns 中）返回 None。"""
    if "predicate" in missing_columns:
        return None
    return Counter(r["predicate"] or "(空)" for r in rows)


# ================================================================ 语句级匹配

def match_statements(a_props: Sequence[dict], b_props: Sequence[dict]) -> dict:
    """单块内命题匹配：归一化后按多重集取交集。

    返回 {"matched": int, "only_a": [原命题 dict], "only_b": [原命题 dict]}；
    only_* 从原命题列表按出现顺序挑出（保留 predicate 等字段供报告展示）。
    """
    a_norm = [normalize_text(p["statement"]) for p in a_props]
    b_norm = [normalize_text(p["statement"]) for p in b_props]
    a_counter, b_counter = Counter(a_norm), Counter(b_norm)
    inter = a_counter & b_counter
    matched = sum(inter.values())

    only_a = _pick_remaining(a_props, a_norm, a_counter - inter)
    only_b = _pick_remaining(b_props, b_norm, b_counter - inter)
    return {"matched": matched, "only_a": only_a, "only_b": only_b}


def _pick_remaining(props: Sequence[dict], norms: list[str],
                    remaining: Counter) -> list[dict]:
    """按出现顺序挑出归一化后仍剩在 remaining 计数中的原命题。"""
    out: list[dict] = []
    for prop, norm in zip(props, norms):
        if remaining.get(norm, 0) > 0:
            remaining[norm] -= 1
            out.append(prop)
    return out


def compare_common_blocks(a_by_chunk: dict[str, list], b_by_chunk: dict[str, list]) -> dict:
    """对全部共同块做语句级匹配。

    返回 {"common_chunks": [...], "matched": int, "only_a": int, "only_b": int,
          "per_block": {chunk_id: {"matched":.., "only_a":[...], "only_b":[...]}}}。
    """
    common = sorted(set(a_by_chunk) & set(b_by_chunk))
    per_block: dict[str, dict] = {}
    matched_total = only_a_total = only_b_total = 0
    for cid in common:
        result = match_statements(a_by_chunk[cid], b_by_chunk[cid])
        per_block[cid] = result
        matched_total += result["matched"]
        only_a_total += len(result["only_a"])
        only_b_total += len(result["only_b"])
    return {"common_chunks": common, "matched": matched_total,
            "only_a": only_a_total, "only_b": only_b_total,
            "per_block": per_block}


def pick_diff_chunks(per_block: dict[str, dict], limit: int) -> list[tuple[str, dict]]:
    """按差异命题数降序、chunk_id 升序取前 limit 个差异块（确定性，可复现）。"""
    diffs = [(cid, data) for cid, data in per_block.items()
             if data["only_a"] or data["only_b"]]
    diffs.sort(key=lambda kv: (
        -(len(kv[1]["only_a"]) + len(kv[1]["only_b"])), kv[0]))
    return diffs[:max(0, limit)]


# ================================================================ summary 指标

def parse_summary_metrics(path: str | Path | None) -> dict:
    """从 summary.md 提取块级指标（extract 的 write_summary 行格式）。

    文件缺失 / 匹配不到的键值为 None（报告渲染为 N/A）。thinking 为 "开"/"关"
    字符串，max_tokens / 块数为 int。
    """
    metrics: dict = {key: None for key in SUMMARY_PATTERNS}
    if not path:
        return metrics
    p = Path(path)
    if not p.is_file():
        warn(f"summary 不存在: {p}，相关指标将标 N/A")
        return metrics
    text = p.read_text(encoding="utf-8", errors="replace")
    for key, pattern in SUMMARY_PATTERNS.items():
        m = re.search(pattern, text)
        if not m:
            continue
        metrics[key] = int(m.group(1)) if key in _INT_KEYS else m.group(1)
    return metrics


# ================================================================ 原文摘录

def fetch_chunk_excerpts(chunks_path: str | Path | None,
                         chunk_ids: Sequence[str], chars: int = 200) -> dict[str, str]:
    """流式扫描 chunks CSV，取指定 chunk 的原文前 chars 个可见字符。

    找齐全部目标块即提前停止（不整表载入，与 extract 的抽样扫描同法）。
    返回 {chunk_id: 摘录}；未出现的块不在结果中（渲染为 N/A）。
    空白折叠为单空格，避免多行原文破坏报告的 Markdown 列表缩进。
    """
    if not chunks_path:
        return {}
    p = Path(chunks_path)
    if not p.is_file():
        warn(f"chunks 文件不存在: {p}，差异块原文将标 N/A")
        return {}
    wanted = set(chunk_ids)
    excerpts: dict[str, str] = {}
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            cid = (row.get("chunk_id") or "").strip()
            if cid in wanted and cid not in excerpts:
                collapsed = " ".join((row.get("content") or "").split())
                excerpts[cid] = collapsed[:chars]
                if len(excerpts) == len(wanted):
                    break
    return excerpts


# ================================================================ 报告渲染

def _md_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _fmt_pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "0.0%"


def _na_if_none(value, suffix: str = "") -> str:
    return f"N/A{suffix}" if value is None else f"{value}{suffix}"


def render_report(*, label_a: str, label_b: str, path_a: str, path_b: str,
                  path_a_summary: str | None, path_b_summary: str | None,
                  group_a: dict, group_b: dict,
                  a_metrics: dict, b_metrics: dict,
                  sample_chunks: int,
                  excerpts: dict[str, str] | None = None) -> str:
    """渲染 Markdown 对比报告（不写文件，便于单测断言）。"""
    excerpts = excerpts or {}
    a_rows, b_rows = group_a["rows"], group_b["rows"]
    a_by = group_by_chunk(a_rows)
    b_by = group_by_chunk(b_rows)
    align = align_blocks(a_by, b_by)
    both_empty, both_empty_note = estimate_both_empty(a_metrics, b_metrics,
                                                      align["union"])

    # ---- 共同块密度与长度分布
    common = sorted(set(a_by) & set(b_by))
    a_common_props = sum(len(a_by[cid]) for cid in common)
    b_common_props = sum(len(b_by[cid]) for cid in common)
    a_len = percentile_nearest_rank(
        [len(r["statement"]) for r in a_rows if r["statement"]])
    b_len = percentile_nearest_rank(
        [len(r["statement"]) for r in b_rows if r["statement"]])

    # ---- 谓词分布
    a_pred = predicate_counts(a_rows, group_a["missing_columns"])
    b_pred = predicate_counts(b_rows, group_b["missing_columns"])

    # ---- 语句级匹配
    statement_ok = ("statement" not in group_a["missing_columns"]
                    and "statement" not in group_b["missing_columns"])
    matching = compare_common_blocks(a_by, b_by) if statement_ok else None

    lines: list[str] = []
    lines.append("# 思考链开/关 A/B 质量对比报告")
    lines.append("")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- A 组（{label_a}）：{path_a}"
                 + (f"（summary：{path_a_summary}）" if path_a_summary else "（未提供 summary）"))
    lines.append(f"- B 组（{label_b}）：{path_b}"
                 + (f"（summary：{path_b_summary}）" if path_b_summary else "（未提供 summary）"))
    lines.append(f"- A 组参数：思考链 {_na_if_none(a_metrics.get('thinking'))}，"
                 f"max_tokens {_na_if_none(a_metrics.get('max_tokens'))}"
                 "（来自 summary，缺失为 N/A）")
    lines.append(f"- B 组参数：思考链 {_na_if_none(b_metrics.get('thinking'))}，"
                 f"max_tokens {_na_if_none(b_metrics.get('max_tokens'))}"
                 "（来自 summary，缺失为 N/A）")
    lines.append("")

    # ---- 1. 块级对齐
    lines.append("## 块级对齐")
    lines.append("")
    lines.append(_md_table(
        ["对齐情况", "块数", "说明"],
        [("两边都有命题", align["both"], "共同块（后续密度/匹配均基于此）"),
         (f"仅 {label_a}", align["only_a"], "只出现在 A 组 CSV"),
         (f"仅 {label_b}", align["only_b"], "只出现在 B 组 CSV"),
         ("两边都空", _na_if_none(both_empty), both_empty_note)]))
    lines.append("")

    # ---- 2. 密度对比
    lines.append("## 密度对比")
    lines.append("")
    lines.append(_md_table(
        ["指标", label_a, label_b],
        [("命题总数（CSV 全部行）", len(a_rows), len(b_rows)),
         ("共同块数", len(common), len(common)),
         ("共同块上命题数", a_common_props, b_common_props),
         ("每块平均命题数（共同块）",
          f"{a_common_props / len(common):.2f}" if common else "N/A",
          f"{b_common_props / len(common):.2f}" if common else "N/A"),
         ("statement 长度 P25",
          _na_if_none(a_len and a_len["p25"]), _na_if_none(b_len and b_len["p25"])),
         ("statement 长度 P50（中位）",
          _na_if_none(a_len and a_len["p50"]), _na_if_none(b_len and b_len["p50"])),
         ("statement 长度 P75",
          _na_if_none(a_len and a_len["p75"]), _na_if_none(b_len and b_len["p75"]))]))
    lines.append("")

    # ---- 3. 谓词分布对比
    lines.append("## 谓词分布对比")
    lines.append("")
    if a_pred is None or b_pred is None:
        lines.append("N/A（某组 CSV 缺少 predicate 列）")
    else:
        predicates = sorted(set(a_pred) | set(b_pred),
                            key=lambda p: (-abs(a_pred[p] - b_pred[p]), p))
        lines.append(_md_table(
            ["谓词", f"{label_a} 数量", f"{label_a} 占比", f"{label_b} 数量",
             f"{label_b} 占比", "差值（A−B）"],
            [(p, a_pred[p], _fmt_pct(a_pred[p], len(a_rows)),
              b_pred[p], _fmt_pct(b_pred[p], len(b_rows)),
              a_pred[p] - b_pred[p]) for p in predicates]))
    lines.append("")

    # ---- 4. 稳定性
    lines.append("## 稳定性")
    lines.append("")
    lines.append(_md_table(
        ["指标", label_a, label_b],
        [("parse_error 块数（summary）",
          _na_if_none(a_metrics.get("parse_error"), " 块"),
          _na_if_none(b_metrics.get("parse_error"), " 块")),
         ("failed 块数（summary）",
          _na_if_none(a_metrics.get("failed"), " 块"),
          _na_if_none(b_metrics.get("failed"), " 块")),
         ("空抽块数（summary）",
          _na_if_none(a_metrics.get("empty_blocks"), " 块"),
          _na_if_none(b_metrics.get("empty_blocks"), " 块")),
         ("抽样块数（summary）",
          _na_if_none(a_metrics.get("sampled_blocks")),
          _na_if_none(b_metrics.get("sampled_blocks")))]))
    lines.append("")

    # ---- 5. 同块命题对齐
    lines.append("## 同块命题对齐（语句级匹配）")
    lines.append("")
    lines.append("- 匹配口径：statement 归一化后（NFKC 全角→半角 + 中文标点映射 + "
                 "ASCII 小写 + 空白折叠；复用 scripts/score_testset.py 的 "
                 "normalize_text）按多重集取交集，即标点/空白差异不视为不同命题。")
    if matching is None:
        lines.append("- N/A：某组 CSV 缺少 statement 列，无法做语句级匹配。")
    else:
        total_joined = matching["matched"] + matching["only_a"] + matching["only_b"]
        match_rate = (f"{matching['matched'] / total_joined:.1%}"
                      if total_joined else "N/A")
        lines.append(_md_table(
            ["统计", "数量", "占比（对匹配+仅A+仅B 总数）"],
            [("完全匹配（A∩B）", matching["matched"],
              _fmt_pct(matching["matched"], total_joined)),
             (f"仅 {label_a}", matching["only_a"],
              _fmt_pct(matching["only_a"], total_joined)),
             (f"仅 {label_b}", matching["only_b"],
              _fmt_pct(matching["only_b"], total_joined)),
             ("合计", total_joined, "100.0%")]))
        lines.append(f"- 共同块 {len(matching['common_chunks'])} 个中，"
                     f"完全一致率（匹配数 / 参与匹配命题总数）：{match_rate}。")
    lines.append("")

    # ---- 6. 差异块抽样
    lines.append(f"## 差异块抽样（前 {sample_chunks} 个，按差异命题数降序）")
    lines.append("")
    if matching is None:
        lines.append("N/A（无语句级匹配结果）")
    else:
        diffs = pick_diff_chunks(matching["per_block"], sample_chunks)
        if not diffs:
            lines.append("（共同块内没有差异命题：两组命题集合完全一致）")
        for i, (cid, data) in enumerate(diffs, 1):
            field = (a_by[cid][0].get("source_field") if a_by[cid]
                     and a_by[cid][0].get("source_field")
                     else (b_by[cid][0].get("source_field") if b_by[cid] else ""))
            lines.append(f"### {i}. {cid}"
                         + (f"（source_field={field}）" if field else ""))
            lines.append("")
            excerpt = excerpts.get(cid)
            lines.append(f"- 原文前 200 字：{excerpt if excerpt else 'N/A（未提供 --chunks 或块未命中）'}")
            lines.append(f"- 仅 {label_a}（{len(data['only_a'])} 条）：")
            for j, prop in enumerate(data["only_a"], 1):
                lines.append(f"  {j}. [{prop.get('predicate') or 'N/A'}] "
                             f"{prop.get('statement') or '(空)'}")
            lines.append(f"- 仅 {label_b}（{len(data['only_b'])} 条）：")
            for j, prop in enumerate(data["only_b"], 1):
                lines.append(f"  {j}. [{prop.get('predicate') or 'N/A'}] "
                             f"{prop.get('statement') or '(空)'}")
            lines.append("")
    lines.append("")

    # ---- 7. 小结
    lines.append("## 小结")
    lines.append("")
    lines.append("以下仅摆齐关键数字，不替代人工判断；建议结合\"差异块抽样\"逐块判断哪版更优。")
    lines.append("")
    if matching is None:
        summary_rows: list[tuple] = [
            ("共同块数（两边都有命题）", align["both"]),
            ("命题总数 A / B", f"{len(a_rows)} / {len(b_rows)}"),
        ]
    else:
        total_joined = matching["matched"] + matching["only_a"] + matching["only_b"]
        summary_rows = [
            ("共同块数（两边都有命题）", align["both"]),
            ("命题总数 A / B", f"{len(a_rows)} / {len(b_rows)}"),
            ("共同块上每块平均 A / B",
             f"{a_common_props / len(common):.2f} / "
             f"{b_common_props / len(common):.2f}" if common else "N/A"),
            ("statement 中位长度 A / B",
             f"{_na_if_none(a_len and a_len['p50'])} / "
             f"{_na_if_none(b_len and b_len['p50'])}"),
            ("语句级完全匹配率",
             f"{matching['matched'] / total_joined:.1%}" if total_joined else "N/A"),
            ("仅 A / 仅 B 命题数",
             f"{matching['only_a']} / {matching['only_b']}"),
        ]
    for name, value in summary_rows:
        lines.append(f"- {name}：{value}")
    lines.append(f"- 稳定性（parse_error / failed）："
                 f"A {_na_if_none(a_metrics.get('parse_error'))}/"
                 f"{_na_if_none(a_metrics.get('failed'))}，"
                 f"B {_na_if_none(b_metrics.get('parse_error'))}/"
                 f"{_na_if_none(b_metrics.get('failed'))}（N/A 为 summary 缺失）")
    lines.append(f"- 两边都空块数：{_na_if_none(both_empty)}（{both_empty_note}）")
    lines.append("")
    return "\n".join(lines)


# ================================================================ CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="compare_thinking_ab",
        description="思考链开/关 A/B 抽取质量对比：块级对齐 / 密度 / 谓词分布 / "
                    "稳定性 / 同块语句级匹配 / 差异块抽样 → Markdown 报告")
    parser.add_argument("--a", required=True, metavar="CSV",
                        help="A 组命题 CSV（开思考跑出）")
    parser.add_argument("--b", required=True, metavar="CSV",
                        help="B 组命题 CSV（关思考跑出）")
    parser.add_argument("--a-summary", metavar="MD", default=None,
                        help="A 组 summary.md（可选；缺 parse_error/failed 等标 N/A）")
    parser.add_argument("--b-summary", metavar="MD", default=None,
                        help="B 组 summary.md（可选）")
    parser.add_argument("--report", required=True, metavar="MD",
                        help="输出 Markdown 对比报告路径")
    parser.add_argument("--chunks", metavar="CSV", default=None,
                        help="documents_chunks.csv（可选；差异块原文前 200 字来源，"
                             "流式扫描找齐即停）")
    parser.add_argument("--label-a", default="开思考(A)", metavar="TEXT",
                        help="A 组标签（默认 \"开思考(A)\"）")
    parser.add_argument("--label-b", default="关思考(B)", metavar="TEXT",
                        help="B 组标签（默认 \"关思考(B)\"）")
    parser.add_argument("--sample-chunks", type=int, default=15, metavar="N",
                        help="差异块抽样展示数（默认 15，按差异命题数降序）")
    parser.add_argument("--excerpt-chars", type=int, default=200, metavar="N",
                        help="差异块原文摘录长度（默认 200 字符）")
    args = parser.parse_args(argv)
    if args.sample_chunks < 0:
        parser.error("--sample-chunks 必须 >= 0")
    if args.excerpt_chars < 1:
        parser.error("--excerpt-chars 必须 >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        group_a = load_props(args.a)
        group_b = load_props(args.b)
    except CompareError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    a_metrics = parse_summary_metrics(args.a_summary)
    b_metrics = parse_summary_metrics(args.b_summary)

    # 差异块原文摘录：先算匹配与差异块，再按需扫 chunks（找齐即停）
    a_by = group_by_chunk(group_a["rows"])
    b_by = group_by_chunk(group_b["rows"])
    statement_ok = ("statement" not in group_a["missing_columns"]
                    and "statement" not in group_b["missing_columns"])
    excerpts: dict[str, str] = {}
    if args.chunks and statement_ok:
        matching = compare_common_blocks(a_by, b_by)
        diff_ids = [cid for cid, _ in
                    pick_diff_chunks(matching["per_block"], args.sample_chunks)]
        excerpts = fetch_chunk_excerpts(args.chunks, diff_ids,
                                        chars=args.excerpt_chars)

    report = render_report(
        label_a=args.label_a, label_b=args.label_b,
        path_a=args.a, path_b=args.b,
        path_a_summary=args.a_summary, path_b_summary=args.b_summary,
        group_a=group_a, group_b=group_b,
        a_metrics=a_metrics, b_metrics=b_metrics,
        sample_chunks=args.sample_chunks, excerpts=excerpts)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"[对比] 报告已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
