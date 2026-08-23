#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务问答 CLI（RAG 端到端）
==========================
用法：
  python qa/ask.py "申领居住证需要提交哪些材料？"
  python qa/ask.py --debug "申领居住证需要提交哪些材料？"   # 打印检索上下文
  python qa/ask.py                                          # 交互模式
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retriever import GovRetriever
from generator import GovGenerator


def ask(question: str, debug: bool = False) -> dict:
    t0 = time.time()
    retriever = GovRetriever()
    try:
        ret = retriever.retrieve(question)
    finally:
        retriever.close()
    t1 = time.time()

    context = ret.to_prompt_context()
    if debug:
        print("=" * 70)
        print(context)
        print("=" * 70)

    gen = GovGenerator()
    out = gen.generate(question, context)
    t2 = time.time()

    return {
        "question": question,
        "answer": out["answer"],
        "usage": out["usage"],
        "seeds": ret.seeds,
        "retrieve_ms": int((t1 - t0) * 1000),
        "generate_ms": int((t2 - t1) * 1000),
    }


def print_result(r: dict):
    print(f"\n{r['answer']}")
    print("\n" + "-" * 70)
    top = ", ".join(f"{s.name}({s.score:.2f})" for s in r["seeds"][:3])
    print(f"命中：{top}")
    print(f"耗时：检索 {r['retrieve_ms']}ms / 生成 {r['generate_ms']}ms；tokens：{r['usage']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--debug", action="store_true", help="打印检索上下文")
    args = ap.parse_args()

    if args.question:
        print_result(ask(args.question, debug=args.debug))
        return

    print("政务问答（输入问题回车，q 退出）")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in {"q", "quit", "exit"}:
            break
        try:
            print_result(ask(q, debug=args.debug))
        except Exception as e:
            print(f"[错误] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
