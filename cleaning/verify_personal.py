#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清洗结果独立校验: 重新解析全部清洗输出, 核对键结构、全局编码唯一性与行数对账。"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {'schema_version', '事项', '办理', '办理结果', '常见问答', '来源', '法律依据', '申请'}
REQUIRED = {"事项", "办理", "办理结果", "常见问答", "来源", "法律依据", "申请"}


def main():
    clean_dir = ROOT / "data" / "cleaned" / "个人服务"
    rej_path = ROOT / "data" / "rejects" / "个人服务_rejects.jsonl"
    report_path = ROOT / "cleaning" / "reports" / "personal_clean_report.json"

    out_total = 0
    key_bad = []
    codes = Counter()
    empty_code = 0
    empty_name = 0
    per_shard = {}
    for shard in sorted(clean_dir.glob("*.jsonl")):
        n = 0
        with open(shard, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                obj = json.loads(line)  # 解析失败即抛异常
                keys = set(obj.keys())
                if not REQUIRED.issubset(keys):
                    key_bad.append((shard.name, line_no, sorted(REQUIRED - keys)))
                sx = obj.get("事项") or {}
                code = sx.get("编码") or ""
                name = sx.get("名称") or ""
                if not code.strip():
                    empty_code += 1
                if not name.strip():
                    empty_name += 1
                if code:
                    codes[code] += 1
                n += 1
        per_shard[shard.name] = n
        out_total += n
        print(f"校验 {shard.name}: {n:,} 行 OK", flush=True)

    dup_left = {c: n for c, n in codes.items() if n > 1}

    rej_total = 0
    rej_reasons = Counter()
    with open(rej_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            assert "_reject_reason" in obj
            rej_reasons[obj["_reject_reason"]] += 1
            rej_total += 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    g = report["global"]

    print("\n===== 校验汇总 =====")
    print(f"清洗输出总行数: {out_total:,} (报告值 {g['output_records']:,})")
    print(f"rejects 总行数: {rej_total:,} (报告 拒绝 {g['rejected']:,} + 重复 {g['duplicates']:,} = {g['rejected'] + g['duplicates']:,})")
    print(f"对账: 输出+拒绝+重复 = {out_total + g['rejected'] + g['duplicates']:,} (输入 {g['input_lines']:,})")
    print(f"输出内重复编码数: {len(dup_left)}")
    print(f"输出内空编码/空名称: {empty_code}/{empty_name}")
    print(f"缺键记录数: {len(key_bad)}")
    print(f"rejects 原因分布: {dict(rej_reasons)}")
    ok = (
        out_total == g["output_records"]
        and rej_total == g["rejected"] + g["duplicates"]
        and out_total + g["rejected"] + g["duplicates"] == g["input_lines"]
        and not dup_left and empty_code == 0 and empty_name == 0 and not key_bad
    )
    print("校验结果:", "全部通过" if ok else "存在不一致, 见上")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
