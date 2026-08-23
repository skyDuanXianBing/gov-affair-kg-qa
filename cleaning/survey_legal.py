#!/usr/bin/env python3
"""字段勘察:对法人服务 JSONL 抽样,统计各表字段名/非空率/代码-TEXT 对照。"""
import json
import os
import sys
from collections import defaultdict

BASE = "/Volumes/f/AllMyData/MyUnderGraduate/政务大模型/data/法人服务"
SAMPLE_FILES = ["交通运输.jsonl", "设立变更.jsonl", "其他.jsonl", "食品药品.jsonl", "司法公证.jsonl"]
N_SAMPLE = 300  # 每文件抽样行数


def is_empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "") or (isinstance(v, (list, dict)) and len(v) == 0)


def main():
    # table -> field -> [nonempty, total]
    field_stats = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    # table -> field -> {code: set(text)}
    code_text = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    tables_seen = defaultdict(int)  # table -> rows having non-empty table
    rows_total = 0
    per_file_rows = {}

    for fname in SAMPLE_FILES:
        path = os.path.join(BASE, fname)
        cnt = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if cnt >= N_SAMPLE:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                cnt += 1
                rows_total += 1
                for tbl, val in obj.items():
                    if isinstance(val, list):
                        if val:
                            tables_seen[tbl] += 1
                        items = val if val else []
                        for it in items[:50]:
                            if not isinstance(it, dict):
                                continue
                            for k, v in it.items():
                                st = field_stats[tbl][k]
                                st[1] += 1
                                if not is_empty(v):
                                    st[0] += 1
                                # code/text 对照:XXX 与 XXX_TEXT
                                if k.endswith("_TEXT") and isinstance(v, str):
                                    base_k = k[:-5]
                                    if base_k in it:
                                        cv = it.get(base_k)
                                        if isinstance(cv, (str, int)):
                                            code_text[tbl][base_k][str(cv)].add(v)
                    elif isinstance(val, dict):
                        tables_seen[tbl] += 1
                        for k, v in val.items():
                            st = field_stats[tbl][k]
                            st[1] += 1
                            if not is_empty(v):
                                st[0] += 1
                    else:
                        # 标量顶层键
                        st = field_stats["<TOP_SCALAR>"][tbl]
                        st[1] += 1
                        if not is_empty(val):
                            st[0] += 1
        per_file_rows[fname] = cnt
        print(f"sampled {fname}: {cnt} rows", file=sys.stderr)

    out = {"per_file_rows": per_file_rows, "rows_total": rows_total,
           "tables_seen": dict(tables_seen), "fields": {}, "code_text": {}}
    for tbl in sorted(field_stats):
        out["fields"][tbl] = {
            k: {"nonempty": v[0], "total": v[1], "rate": round(v[0] / v[1], 4) if v[1] else 0}
            for k, v in sorted(field_stats[tbl].items())
        }
    for tbl in sorted(code_text):
        out["code_text"][tbl] = {
            k: {c: sorted(ts)[:5] for c, ts in sorted(v.items())}
            for k, v in sorted(code_text[tbl].items())
        }

    os.makedirs("/Volumes/f/AllMyData/MyUnderGraduate/政务大模型/cleaning/reports", exist_ok=True)
    dst = "/Volumes/f/AllMyData/MyUnderGraduate/政务大模型/cleaning/reports/legal_survey_raw.json"
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
