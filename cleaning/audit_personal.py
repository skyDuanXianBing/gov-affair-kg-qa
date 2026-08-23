#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人服务清洗输出质量审计: HTML 实体 / HTML 标签 / 控制字符 残留统计 (仅标准库, 流式)。

输出 cleaning/reports/personal_quality_audit.json, 并打印汇总。
"""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLEAN_DIR = ROOT / "data" / "cleaned" / "个人服务"
REPORT = ROOT / "cleaning" / "reports" / "personal_quality_audit.json"

ENTITIES = ("&lt;", "&gt;", "&quot;", "&amp;", "&nbsp;")
ENTITY_GENERIC_RE = re.compile(r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)[^>]*>")
C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # 不含 \t \n


def scan_text(s, path, c):
    for ent in ENTITIES:
        n = s.count(ent)
        if n:
            c["entity"][ent] += n
            c["entity_by_path"][f"{ent} @{path}"] += n
    generics = 0
    for m in ENTITY_GENERIC_RE.finditer(s):
        g = m.group(0)
        if g not in ENTITIES:
            generics += 1
            c["entity_other_example"][g] += 1
    if generics:
        c["entity"]["<other>"] += generics
        c["entity_by_path"][f"<other> @{path}"] += generics
    for m in TAG_RE.finditer(s):
        tag = m.group(1).lower()
        c["tag"][tag] += 1
        c["tag_by_path"][f"<{tag}> @{path}"] += 1
    n = s.count("\t")
    if n:
        c["tab"]["\\t"] += n
        c["tab_by_path"][path] += n
    n = s.count("\x7f")
    if n:
        c["del"]["\\x7f"] += n
        c["del_by_path"][path] += n
    hits = C0_RE.findall(s)
    if hits:
        for ch in hits:
            c["c0"][f"\\x{ord(ch):02x}"] += 1
        c["c0_by_path"][path] += len(hits)


def walk(node, path, c):
    if isinstance(node, str):
        scan_text(node, path, c)
    elif isinstance(node, dict):
        for k, v in node.items():
            walk(v, f"{path}.{k}", c)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                for k, v in item.items():
                    walk(v, f"{path}.{k}", c)
            elif isinstance(item, str):
                scan_text(item, path, c)


def main():
    shards = sorted(CLEAN_DIR.glob("*.jsonl"))
    if not shards:
        print(f"错误: 无分片 {CLEAN_DIR}", file=sys.stderr)
        sys.exit(1)
    c = {
        "entity": Counter(), "entity_by_path": Counter(), "entity_other_example": Counter(),
        "tag": Counter(), "tag_by_path": Counter(),
        "tab": Counter(), "tab_by_path": Counter(),
        "del": Counter(), "del_by_path": Counter(),
        "c0": Counter(), "c0_by_path": Counter(),
    }
    total_lines = 0
    t0 = time.time()
    for shard in shards:
        n = 0
        with open(shard, encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                walk(obj, "", c)
                n += 1
        total_lines += n
        print(f"[{time.strftime('%H:%M:%S')}] {shard.name}: {n:,} 行, 累计 {total_lines:,}", flush=True)

    def top(counter, k=25):
        return [{"key": key, "count": v} for key, v in counter.most_common(k)]

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - t0, 1),
        "lines_scanned": total_lines,
        "totals": {
            "html_entities": sum(c["entity"].values()),
            "html_tags": sum(c["tag"].values()),
            "tab_chars": sum(c["tab"].values()),
            "del_x7f": sum(c["del"].values()),
            "other_c0": sum(c["c0"].values()),
        },
        "entity_by_name": dict(c["entity"].most_common()),
        "entity_other_examples": dict(c["entity_other_example"].most_common(20)),
        "entity_top_paths": top(c["entity_by_path"]),
        "tag_by_name": dict(c["tag"].most_common()),
        "tag_top_paths": top(c["tag_by_path"]),
        "tab_top_paths": top(c["tab_by_path"]),
        "del_top_paths": top(c["del_by_path"]),
        "c0_by_char": dict(c["c0"].most_common()),
        "c0_top_paths": top(c["c0_by_path"]),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["totals"], ensure_ascii=False), flush=True)
    print(f"审计报告: {REPORT}", flush=True)


if __name__ == "__main__":
    main()
