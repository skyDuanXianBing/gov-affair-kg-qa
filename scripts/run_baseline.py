#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量跑问答基线：读 testset.csv，逐题调用 qa/ask.py 的 ask()，产出预测 JSONL。

输出 JSONL 每行 {"test_id": ..., "answer": ...}（判分器 scripts/score_testset.py
的输入格式）；单题重试后仍失败写 {"test_id": ..., "answer": "", "error": "..."}
（判分器按拒答/空答案计 0 分）。

重要：qa 模块（qa/retriever.py 等）在导入时读取 KG_SCHEMA 等环境变量（默认
govaffair），本脚本不修改任何环境变量，由调用方设置。推荐调用方式：

    # zwdmxgj 图 + KAG 式多跳检索
    KG_SCHEMA=zwdmxgj python scripts/run_baseline.py \\
        --testset data/pilot/testset_multihop.csv \\
        --out data/pilot/baseline_multihop.jsonl --multihop

    # zwdmxgj 图 + 单轮检索（对照组）
    KG_SCHEMA=zwdmxgj python scripts/run_baseline.py \\
        --testset data/pilot/testset_multihop.csv \\
        --out data/pilot/baseline_single.jsonl

断点续跑：--out 中已存在的 test_id 直接跳过（含此前失败的 error 行；如需重跑
失败题，删除 JSONL 中对应行后重跑同一命令即可）。每写一行即 flush，Ctrl-C 后
重跑自动续作。

用法参数：
  --testset CSV   题集（utf-8-sig，需含 test_id 与 question 两列）
  --out JSONL     预测输出（追加写）
  --multihop      开关：ask(question, multihop=True) 走 qa/multihop.py 多跳检索
  --limit N       本次运行最多新跑 N 题（默认不限）
  --retries N     单题异常重试次数（默认 2，即最多调用 1+2 次）
  --sleep SEC     相邻两题间的限速间隔（默认 0.5 秒）

环境变量（由调用方设置，本脚本只透传不修改）：KG_SCHEMA（govaffair | zwdmxgj）
及 qa/retriever.py、qa/generator.py 读取的 NEO4J_*、DEEPSEEK_* 等。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_ID_COL = "test_id"
QUESTION_COL = "question"


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def read_testset(path: str | Path) -> list[dict[str, str]]:
    """读题集为 [{"test_id": ..., "question": ...}, ...]（utf-8-sig，容忍 BOM）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"题集文件不存在：{p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [c for c in (TEST_ID_COL, QUESTION_COL) if c not in fieldnames]
        if missing:
            raise ValueError(f"{p}: 缺少必需列 {missing}，实际列 {fieldnames}")
        rows: list[dict[str, str]] = []
        for line_no, record in enumerate(reader, 2):
            test_id = (record.get(TEST_ID_COL) or "").strip()
            question = (record.get(QUESTION_COL) or "").strip()
            if not test_id:
                raise ValueError(f"{p}: 第 {line_no} 行 test_id 为空")
            if not question:
                raise ValueError(f"{p}: 第 {line_no} 行 question 为空")
            rows.append({TEST_ID_COL: test_id, QUESTION_COL: question})
    if not rows:
        raise ValueError(f"{p}: 没有读到任何题目记录")
    return rows


def load_done_ids(path: str | Path) -> set[str]:
    """读取已有预测 JSONL 的 test_id 集合（坏行忽略并告警，用于断点续跑）。"""
    p = Path(path)
    done: set[str] = set()
    if not p.is_file():
        return done
    with p.open(encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"警告: {p}:{line_no}: JSON 解析失败（忽略该行，视作未完成）",
                      file=sys.stderr)
                continue
            if isinstance(obj, dict) and str(obj.get(TEST_ID_COL) or "").strip():
                done.add(str(obj[TEST_ID_COL]).strip())
    return done


def load_ask() -> Callable[..., dict]:
    """延迟导入 qa/ask.py 的 ask()（导入时才读取 KG_SCHEMA 等环境变量）。

    ask 签名（qa/ask.py 实测）：ask(question: str, debug: bool = False,
    multihop: bool = False) -> dict，返回结构含字符串字段 "answer"。
    """
    qa_dir = str(REPO_ROOT / "qa")
    if qa_dir not in sys.path:
        sys.path.insert(0, qa_dir)
    import ask as ask_module  # noqa: E402

    return ask_module.ask


def ask_one(
    ask_fn: Callable[..., dict],
    question: str,
    multihop: bool,
    retries: int,
) -> dict:
    """单题调用（最多 1+retries 次）。成功 → {"answer", "attempts"}；
    重试耗尽 → {"answer": "", "error", "attempts"}。"""
    last_error = ""
    for attempt in range(1, retries + 2):
        try:
            result = ask_fn(question, multihop=multihop)
            if not isinstance(result, dict):
                raise ValueError(f"ask() 返回类型应为 dict，实际 {type(result).__name__}")
            answer = result.get("answer")
            if not isinstance(answer, str):
                raise ValueError("ask() 返回结构缺少字符串 answer 字段")
            return {"answer": answer, "attempts": attempt}
        except Exception as e:  # noqa: BLE001 —— 单题失败不应中断整批
            last_error = f"{type(e).__name__}: {e}"
    return {"answer": "", "error": last_error, "attempts": retries + 1}


def run(
    rows: list[dict[str, str]],
    out_path: str | Path,
    ask_fn: Callable[..., dict],
    *,
    multihop: bool = False,
    limit: int | None = None,
    retries: int = 2,
    sleep: float = 0.5,
    log: Callable[[str], None] = _default_log,
) -> dict[str, int]:
    """逐题调用 ask_fn 并追加写 JSONL。返回统计 {total, skipped_done, ok, failed, retried}。"""
    done = load_done_ids(out_path)
    stats = {"total": len(rows), "skipped_done": 0, "ok": 0, "failed": 0, "retried": 0}
    written = 0
    with open(out_path, "a", encoding="utf-8") as fh:
        for index, row in enumerate(rows, 1):
            test_id = row[TEST_ID_COL]
            if test_id in done:
                stats["skipped_done"] += 1
                continue
            if limit is not None and written >= limit:
                break
            outcome = ask_one(ask_fn, row[QUESTION_COL], multihop, retries)
            record = {TEST_ID_COL: test_id, "answer": outcome["answer"]}
            if "error" in outcome:
                record["error"] = outcome["error"]
                stats["failed"] += 1
            else:
                stats["ok"] += 1
            stats["retried"] += outcome["attempts"] - 1
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            done.add(test_id)
            written += 1
            if written % 5 == 0 or index == len(rows):
                log(f"[{index}/{len(rows)}] 本次已写入 {written} 条"
                    f"（ok={stats['ok']} failed={stats['failed']}）")
            if sleep > 0:
                time.sleep(sleep)
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_baseline",
        description="批量跑问答基线：testset.csv → 逐题 ask() → 预测 JSONL（断点续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="推荐调用方式（qa 模块导入时读取 KG_SCHEMA，由调用方设置）：\n"
               "  KG_SCHEMA=zwdmxgj python scripts/run_baseline.py "
               "--testset data/pilot/testset_multihop.csv "
               "--out data/pilot/baseline_multihop.jsonl --multihop",
    )
    parser.add_argument("--testset", required=True, metavar="CSV",
                        help="题集 CSV（utf-8-sig，需含 test_id 与 question 列）")
    parser.add_argument("--out", required=True, metavar="JSONL",
                        help="预测输出 JSONL（追加写，已有 test_id 跳过）")
    parser.add_argument("--multihop", action="store_true",
                        help="ask(question, multihop=True)，走 qa/multihop.py 多跳检索")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="本次运行最多新跑 N 题（默认不限）")
    parser.add_argument("--retries", type=int, default=2, metavar="N",
                        help="单题异常重试次数（默认 2）")
    parser.add_argument("--sleep", type=float, default=0.5, metavar="SEC",
                        help="相邻两题间隔秒数（默认 0.5，限速用）")
    args = parser.parse_args(argv)
    if args.retries < 0:
        parser.error("--retries 必须 >= 0")
    if args.sleep < 0:
        parser.error("--sleep 必须 >= 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须 >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = read_testset(args.testset)
    except (OSError, ValueError) as e:
        print(f"[错误] 读取题集失败：{e}", file=sys.stderr)
        return 2

    ask_fn = load_ask()
    mode = "多跳检索（multihop）" if args.multihop else "单轮检索"
    print(f"# 题集 {args.testset}（{len(rows)} 题）→ {args.out}；{mode}；"
          f"retries={args.retries} sleep={args.sleep}s limit={args.limit}")
    try:
        stats = run(rows, args.out, ask_fn, multihop=args.multihop,
                    limit=args.limit, retries=args.retries, sleep=args.sleep)
    except KeyboardInterrupt:
        print("\n[中断] 已写入行均已落盘，重跑同一命令将跳过已完成题目。",
              file=sys.stderr)
        return 130

    print(f"# 完成：题集 {stats['total']} 题（断点跳过 {stats['skipped_done']}）"
          f"→ ok={stats['ok']} failed={stats['failed']} 重试 {stats['retried']} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
