#!/usr/bin/env python3
"""testset 判分脚本：对预测 JSONL 与 testset.csv 做 EM / 字符级 F1 统计。

输入：
  --testset  一个或多个 testset.csv（utf-8-sig，容忍 BOM 与多行引号字段）
             列：test_id,question,expected_answer,doc_id,title,category_l1,category_l2,
                 service_id,department_name,answer_type,source_url,source_tables
  --pred     预测 JSONL，每行 {"test_id": "...", "answer": "..."}；
             缺失 test_id 的题目记为未答（计 0 分，单列）；answer 为空记为拒答/空答案（计 0 分，单列）
  --json     输出机器可读 JSON（默认输出人类可读报表）

指标：
  EM   归一化后精确匹配（去首尾空白、全角/半角标点统一、空白折叠、ASCII 小写）
  F1   字符级 F1（参考 SQuAD/HotpotQA token F1：中文按单字切分，
       连续 ASCII 字母数字串合并为一个 token，标点不参与计算）

说明：
  两域 answer_type 枚举不统一（pilot: materials/department_materials/process/
  condition_result/legal_basis；personal: materials/condition/process/time_limit/
  channel/result/legal_basis/faq/fee/department），本脚本按各自原样分组统计，
  不做归并；归并参考见输出中的"枚举说明"。

自测（在仓库根目录执行；用 testset 自身答案构造 mock 预测）：

  # Mock 1：完美预测（每题 answer=expected_answer），期望总体 EM=100%、F1=1.0
  python3 -c '
  import csv, json
  out = open("/tmp/mock_perfect.jsonl", "w", encoding="utf-8")
  for name in ("pilot", "personal"):
      with open("data/%s/testset.csv" % name, encoding="utf-8-sig", newline="") as f:
          for row in csv.DictReader(f):
              rec = {"test_id": row["test_id"], "answer": row["expected_answer"]}
              out.write(json.dumps(rec, ensure_ascii=False) + "\n")
  out.close()
  '
  python3 scripts/score_testset.py \
      --testset data/pilot/testset.csv data/personal/testset.csv \
      --pred /tmp/mock_perfect.jsonl

  # Mock 2：每个数据集内隔一题答错（0 基偶数序=正确，奇数序=固定错误文本）
  # 期望：pilot EM=3/5=0.6，personal EM=5/10=0.5，总体 EM=8/15≈0.5333
  python3 -c '
  import csv, json
  out = open("/tmp/mock_half.jsonl", "w", encoding="utf-8")
  for name in ("pilot", "personal"):
      with open("data/%s/testset.csv" % name, encoding="utf-8-sig", newline="") as f:
          for i, row in enumerate(csv.DictReader(f)):
              answer = row["expected_answer"] if i % 2 == 0 else "故意答错的文本"
              rec = {"test_id": row["test_id"], "answer": answer}
              out.write(json.dumps(rec, ensure_ascii=False) + "\n")
  out.close()
  '
  python3 scripts/score_testset.py \
      --testset data/pilot/testset.csv data/personal/testset.csv \
      --pred /tmp/mock_half.jsonl

退出码：0 正常判分；2 输入错误（文件缺失、预测文件为空、testset 缺列/test_id 重复等）。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = ("test_id", "expected_answer", "answer_type")

# NFKC 之后仍不折叠的中文标点，映射到对应半角形式
PUNCT_MAP = {
    "，": ",",
    "、": ",",
    "。": ".",
    "；": ";",
    "：": ":",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "《": '"',
    "》": '"',
    "—": "-",
    "～": "~",
}

# 连续 ASCII 字母数字串作为一个 token；其余非空白字符按单字切（标点随后丢弃）
TOKEN_RE = re.compile(r"[a-z0-9]+|[^a-z0-9\s]")

ANSWER_TYPE_MERGE_HINTS = {
    "department_materials": "≈ department + materials",
    "condition_result": "≈ condition + result",
}


class ScoreError(Exception):
    """输入错误，应导致非 0 退出码。"""


def warn(message: str) -> None:
    print(f"警告: {message}", file=sys.stderr)


def normalize_text(text: str | None) -> str:
    """判分用归一化：NFKC（全角→半角）+ 中文标点映射 + ASCII 小写 + 空白折叠。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(PUNCT_MAP.get(ch, ch) for ch in text)
    text = text.lower()
    return " ".join(text.split())


def tokenize(normalized: str) -> list[str]:
    """中文按单字、连续 ASCII 字母数字串合并为单 token；标点与空白丢弃。"""
    return [tok for tok in TOKEN_RE.findall(normalized) if tok.isalnum()]


def exact_match(pred_answer: str | None, gold_answer: str | None) -> int:
    return 1 if normalize_text(pred_answer) == normalize_text(gold_answer) else 0


def char_f1(pred_tokens: list[str], gold_tokens: list[str]) -> float:
    """多重集交上的 token 级 F1（SQuAD 风格）；两侧皆空记 1.0，单侧空记 0。"""
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    num_same = sum((Counter(pred_tokens) & Counter(gold_tokens)).values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def answer_f1(pred_answer: str | None, gold_answer: str | None) -> float:
    return char_f1(tokenize(normalize_text(pred_answer)), tokenize(normalize_text(gold_answer)))


def load_testsets(paths: list[Path]) -> list[dict[str, str]]:
    """读取全部 testset（utf-8-sig，多行引号字段由 csv 模块处理）。dataset 名取父目录名。"""
    rows: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            raise ScoreError(f"测试集文件不存在: {path}")
        dataset = path.parent.name or path.stem
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
            if missing:
                raise ScoreError(f"{path}: 缺少必需列 {missing}，实际列 {fieldnames}")
            count = 0
            for record_no, record in enumerate(reader, 2):
                test_id = (record.get("test_id") or "").strip()
                if not test_id:
                    raise ScoreError(f"{path}: 第 {record_no} 条记录 test_id 为空")
                if test_id in seen:
                    raise ScoreError(f"{path}: 第 {record_no} 条记录 test_id 重复 {test_id}（首见于 {seen[test_id]}）")
                seen[test_id] = f"{path} 第 {record_no} 条"
                rows.append(
                    {
                        "test_id": test_id,
                        "dataset": dataset,
                        "question": (record.get("question") or "").strip(),
                        "expected_answer": record.get("expected_answer") or "",
                        "answer_type": (record.get("answer_type") or "").strip() or "(空)",
                    }
                )
                count += 1
        if count == 0:
            raise ScoreError(f"{path}: 没有读到任何题目记录")
    return rows


def load_predictions(path: Path) -> tuple[dict[str, str], dict[str, int]]:
    """读取预测 JSONL，返回 {test_id: answer} 与统计；空文件/无可用行报 ScoreError。"""
    if not path.is_file():
        raise ScoreError(f"预测文件不存在: {path}")
    predictions: dict[str, str] = {}
    total = 0
    skipped = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            total += 1
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                warn(f"{path}:{line_no}: JSON 解析失败（{exc.msg}），已跳过")
                skipped += 1
                continue
            if not isinstance(obj, dict):
                warn(f"{path}:{line_no}: 非对象行，已跳过")
                skipped += 1
                continue
            test_id = obj.get("test_id")
            if test_id is None or not str(test_id).strip():
                warn(f"{path}:{line_no}: 缺少 test_id，已跳过")
                skipped += 1
                continue
            test_id = str(test_id).strip()
            answer = obj.get("answer")
            if answer is None:
                warn(f"{path}:{line_no}: {test_id} 缺少 answer 字段，按空答案（拒答）处理")
                answer = ""
            elif not isinstance(answer, str):
                warn(f"{path}:{line_no}: {test_id} answer 不是字符串，已强制 str() 转换")
                answer = str(answer)
            if test_id in predictions:
                warn(f"{path}:{line_no}: test_id {test_id} 重复，后者覆盖前者")
            predictions[test_id] = answer
    if total == 0:
        raise ScoreError(f"预测文件为空: {path}")
    if not predictions:
        raise ScoreError(f"预测文件中没有可用的预测行: {path}")
    return predictions, {"total": total, "skipped": skipped}


def score_rows(rows: list[dict[str, str]], predictions: dict[str, str]) -> list[dict[str, Any]]:
    """逐题打分。未答（预测缺 test_id）与拒答（answer 归一化后为空）均计 0 分。"""
    items: list[dict[str, Any]] = []
    for row in rows:
        test_id = row["test_id"]
        item: dict[str, Any] = {
            "test_id": test_id,
            "dataset": row["dataset"],
            "answer_type": row["answer_type"],
            "answered": False,
            "refused": False,
            "em": 0,
            "f1": 0.0,
        }
        pred_answer = predictions.get(test_id)
        if pred_answer is None:
            items.append(item)  # 未答
            continue
        if not normalize_text(pred_answer):
            item["refused"] = True
            items.append(item)  # 拒答/空答案
            continue
        item["answered"] = True
        item["em"] = exact_match(pred_answer, row["expected_answer"])
        item["f1"] = answer_f1(pred_answer, row["expected_answer"])
        items.append(item)
    return items


def group_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """组内统计：EM/F1 为组内所有题目的均值（未答/拒答计 0）。"""
    count = len(items)
    em_hits = sum(item["em"] for item in items)
    answered = sum(1 for item in items if item["answered"])
    refused = sum(1 for item in items if item["refused"])
    return {
        "count": count,
        "em_hits": em_hits,
        "em": round(em_hits / count, 6) if count else 0.0,
        "f1": round(sum(item["f1"] for item in items) / count, 6) if count else 0.0,
        "answered": answered,
        "refused": refused,
        "unanswered": count - answered - refused,
    }


def build_result(
    testset_paths: list[Path],
    pred_path: Path,
    rows: list[dict[str, str]],
    predictions: dict[str, str],
    pred_stats: dict[str, int],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    by_answer_type: dict[str, dict[str, Any]] = {}
    by_dataset: dict[str, dict[str, Any]] = {}
    by_dataset_answer_type: dict[str, dict[str, dict[str, Any]]] = {}
    enums_by_dataset: dict[str, list[str]] = {}
    for row in rows:
        enums_by_dataset.setdefault(row["dataset"], [])
        if row["answer_type"] not in enums_by_dataset[row["dataset"]]:
            enums_by_dataset[row["dataset"]].append(row["answer_type"])
    for key in sorted({item["answer_type"] for item in items}):
        by_answer_type[key] = group_summary([i for i in items if i["answer_type"] == key])
    for key in sorted({item["dataset"] for item in items}):
        group_items = [i for i in items if i["dataset"] == key]
        by_dataset[key] = group_summary(group_items)
        by_dataset_answer_type[key] = {
            t: group_summary([i for i in group_items if i["answer_type"] == t])
            for t in sorted({i["answer_type"] for i in group_items})
        }
    return {
        "testsets": [str(p) for p in testset_paths],
        "pred_file": str(pred_path),
        "num_testset_rows": len(rows),
        "num_predictions": pred_stats["total"],
        "num_predictions_skipped": pred_stats["skipped"],
        "num_predictions_unknown": sum(1 for t in predictions if t not in {r["test_id"] for r in rows}),
        "overall": group_summary(items),
        "by_answer_type": by_answer_type,
        "by_dataset": by_dataset,
        "by_dataset_answer_type": by_dataset_answer_type,
        "unanswered": [i["test_id"] for i in items if not i["answered"] and not i["refused"]],
        "refused": [i["test_id"] for i in items if i["refused"]],
        "answer_type_enum_by_dataset": enums_by_dataset,
        "answer_type_merge_hints": ANSWER_TYPE_MERGE_HINTS,
    }


def format_report(result: dict[str, Any]) -> str:
    overall = result["overall"]
    lines: list[str] = []
    lines.append("testset 判分结果")
    lines.append("=" * 64)
    lines.append(f"测试集: {', '.join(result['testsets'])}（共 {result['num_testset_rows']} 题）")
    lines.append(
        f"预测:   {result['pred_file']}"
        f"（{result['num_predictions']} 条，跳过 {result['num_predictions_skipped']} 条，"
        f"未知 test_id {result['num_predictions_unknown']} 条）"
    )
    lines.append("")
    lines.append(
        f"总体: EM={overall['em']:.4f}（{overall['em_hits']}/{overall['count']}）"
        f"  F1={overall['f1']:.4f}"
        f"  已答={overall['answered']}  拒答/空答案={overall['refused']}  未答={overall['unanswered']}"
    )
    lines.append("")
    lines.append("按 answer_type 分组（两域原样，不归并）:")
    for answer_type, summary in result["by_answer_type"].items():
        lines.append(_format_group(f"  {answer_type}", summary))
    lines.append("")
    lines.append("按数据集分组:")
    for dataset, summary in result["by_dataset"].items():
        lines.append(_format_group(f"  {dataset}", summary))
    lines.append("")
    unanswered = result["unanswered"]
    lines.append(f"未答题（{len(unanswered)}）: {', '.join(unanswered) if unanswered else '（无）'}")
    refused = result["refused"]
    lines.append(f"拒答/空答案（{len(refused)}）: {', '.join(refused) if refused else '（无）'}")
    lines.append("")
    lines.append("枚举说明: 两域 answer_type 枚举不统一，本脚本按各自原样分组，不做归并。")
    for dataset, enums in sorted(result["answer_type_enum_by_dataset"].items()):
        lines.append(f"  {dataset}: {', '.join(enums)}")
    hints = "；".join(f"{k} {v}" for k, v in ANSWER_TYPE_MERGE_HINTS.items())
    lines.append(f"  跨域对比归并参考: {hints}")
    return "\n".join(lines)


def _format_group(label: str, summary: dict[str, Any]) -> str:
    return (
        f"{label:<24} count={summary['count']:>3}"
        f"  EM={summary['em']:.4f}（{summary['em_hits']}/{summary['count']}）"
        f"  F1={summary['f1']:.4f}"
        f"  未答={summary['unanswered']}  拒答={summary['refused']}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对 testset.csv 与预测 JSONL 做 EM/F1 判分统计")
    parser.add_argument(
        "--testset",
        nargs="+",
        required=True,
        type=Path,
        metavar="CSV",
        help="一个或多个 testset.csv 路径（utf-8-sig）",
    )
    parser.add_argument(
        "--pred",
        required=True,
        type=Path,
        metavar="JSONL",
        help='预测 JSONL，每行 {"test_id": "...", "answer": "..."}',
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（默认人类可读报表）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = load_testsets(args.testset)
        predictions, pred_stats = load_predictions(args.pred)
    except ScoreError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    known_ids = {row["test_id"] for row in rows}
    unknown = sorted(t for t in predictions if t not in known_ids)
    if unknown:
        preview = ", ".join(unknown[:10])
        warn(f"预测中有 {len(unknown)} 个 test_id 不在任何测试集中，已忽略: {preview}")
    items = score_rows(rows, predictions)
    result = build_result(args.testset, args.pred, rows, predictions, pred_stats, items)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
