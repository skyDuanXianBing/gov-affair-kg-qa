#!/usr/bin/env python3
"""全量物化结果的独立校验(不依赖 materialize_legal.py 的内部统计)。"""
import json
import os
import sys

BASE = "/Volumes/f/AllMyData/MyUnderGraduate/政务大模型"
OUT = os.path.join(BASE, "data", "cleaned", "法人服务")
REJ = os.path.join(BASE, "data", "cleaned", "rejects", "法人服务_rejects.jsonl")
SRC = os.path.join(BASE, "data", "法人服务")

TOP_KEYS = ["schema_version", "事项", "办理", "办理结果", "常见问答", "申请", "法律依据", "来源"]
SHIXIANG_KEYS = ["事项类型", "名称", "官方列表出现次数", "实施主体", "服务对象", "状态", "编码", "行使层级", "详情返回编码", "主题分类"]
BANLI_KEYS = ["办理地址", "办理方式", "办理深度", "办理环节", "可网上办理", "咨询电话", "承诺办结时限", "承诺时限说明", "投诉电话", "是否收费", "法定办结时限", "法定时限说明", "窗口办理流程", "网上办理流程", "网上办理限制说明", "跨域通办"]

total, bad_struct = 0, 0
codes = {}
themes = {}
ctrl_bad = 0
name_empty = code_empty = 0
fill = {k: 0 for k in ["办理地址", "咨询电话", "投诉电话", "承诺办结时限", "法定办结时限", "窗口办理流程", "网上办理流程"]}
fill_mat = fill_law = fill_qa = fill_flow = fill_result = 0
per_file = {}

for fn in sorted(os.listdir(OUT)):
    if not fn.endswith(".jsonl"):
        continue
    cnt = 0
    with open(os.path.join(OUT, fn), encoding="utf-8") as f:
        for line in f:
            total += 1
            cnt += 1
            rec = json.loads(line)
            if list(rec.keys()) != TOP_KEYS:
                bad_struct += 1
                continue
            if list(rec["事项"].keys()) != SHIXIANG_KEYS or list(rec["办理"].keys()) != BANLI_KEYS:
                bad_struct += 1
            if list(rec["申请"].keys()) != ["受理条件", "材料"]:
                bad_struct += 1
            c = rec["事项"]["编码"]
            codes[c] = codes.get(c, 0) + 1
            th = fn[:-5]
            themes.setdefault(c, set()).add(th)
            if not rec["事项"]["名称"]:
                name_empty += 1
            if not c:
                code_empty += 1
            for k in fill:
                if rec["办理"][k]:
                    fill[k] += 1
            if rec["申请"]["材料"]:
                fill_mat += 1
            if rec["法律依据"]:
                fill_law += 1
            if rec["常见问答"]:
                fill_qa += 1
            if rec["办理"]["办理环节"]:
                fill_flow += 1
            if rec["办理结果"]:
                fill_result += 1
            # 残留控制字符检查
            if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in line):
                ctrl_bad += 1
    per_file[fn] = cnt

dups = {c: n for c, n in codes.items() if n > 1}
rej_cnt = rej_reasons = 0
reason_stat = {}
with open(REJ, encoding="utf-8") as f:
    for line in f:
        rej_cnt += 1
        r = json.loads(line).get("_reject_reason", "?")
        reason_stat[r] = reason_stat.get(r, 0) + 1

print(json.dumps({
    "total_output_rows": total,
    "unique_codes": len(codes),
    "dup_codes_in_output": len(dups),
    "bad_struct_rows": bad_struct,
    "name_empty": name_empty,
    "code_empty": code_empty,
    "ctrl_char_lines": ctrl_bad,
    "fill_rates": {k: round(v / total, 4) for k, v in fill.items()},
    "fill_lists": {"材料": round(fill_mat / total, 4), "法律依据": round(fill_law / total, 4),
                   "常见问答": round(fill_qa / total, 4), "办理环节": round(fill_flow / total, 4),
                   "办理结果": round(fill_result / total, 4)},
    "rejects": {"total": rej_cnt, "by_reason": reason_stat},
    "files": len(per_file),
}, ensure_ascii=False, indent=1))
