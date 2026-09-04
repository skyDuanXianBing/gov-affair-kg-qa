#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 迁移预处理：边表按业务键去重，保留首行（列全保留）。

- service_based_on_out.csv   按 (service_id, legal_citation_id) 去重
- service_requires_material_out.csv 按 (service_id, material_id) 去重

原文件改名为 *.full.csv 保留审计；去重产物写回原文件名（manifest 无需改动）。
输出行数统计到 build/shared_ids/pilot/dedup_report.json。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "build" / "shared_ids" / "pilot"

TASKS = {
    "service_based_on_out.csv": ("service_id", "legal_citation_id"),
    "service_requires_material_out.csv": ("service_id", "material_id"),
}


def dedup(name: str, keys: tuple[str, str]) -> dict:
    src = PILOT / name
    tmp = PILOT / (name + ".dedup.tmp")
    keep_full = PILOT / (name + ".full.csv")
    rows_in = rows_out = 0
    seen: set[tuple[str, str]] = set()
    with src.open("r", encoding="utf-8-sig", newline="") as fh, \
            tmp.open("w", encoding="utf-8", newline="") as out:
        reader = csv.reader(fh)
        writer = csv.writer(out, lineterminator="\n")
        header = [c.strip() for c in next(reader)]
        writer.writerow(header)
        idx = [header.index(k) for k in keys]
        for row in reader:
            rows_in += 1
            key = (row[idx[0]], row[idx[1]])
            if key in seen:
                continue
            seen.add(key)
            writer.writerow(row)
            rows_out += 1
    if keep_full.exists():
        keep_full.unlink()
    src.rename(keep_full)
    tmp.rename(src)
    return {"file": name, "keys": list(keys), "rows_in": rows_in,
            "rows_out": rows_out, "removed": rows_in - rows_out}


def main() -> int:
    report = []
    for name, keys in TASKS.items():
        path = PILOT / name
        if not path.exists():
            print(f"SKIP 缺文件: {path}", file=sys.stderr)
            return 1
        stat = dedup(name, keys)
        report.append(stat)
        print(f"{name}: {stat['rows_in']:,} -> {stat['rows_out']:,} "
              f"(去重删除 {stat['removed']:,} 行, {stat['removed']/max(stat['rows_in'],1)*100:.1f}%)")
    out = PILOT / "dedup_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
