#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人服务数据集全量清洗脚本（仅使用 Python 标准库）。

输入:  data/个人服务/*.jsonl  (22 个分片, 每行一个 JSON 对象)
输出:  data/cleaned/个人服务/<原分片名>        清洗后的记录
       data/rejects/个人服务_rejects.jsonl     被拒记录 + 重复记录 (含 _reject_reason)
       cleaning/reports/personal_clean_report.json / .md

清洗规则:
 1. 行必须可解析为 JSON 对象; 顶层 8 个业务键齐全; "事项.编码" 与 "事项.名称" 非空,
    否则进 rejects。
 2. 按 "事项.编码" 全局去重, 保留首次出现的记录; 编码为空的记录用 "名称+实施主体"
    作备选键 (规则 1 已拒绝编码为空者, 该分支为防御性逻辑)。
 3. 字符串规范化 (与法人服务 materialize_legal.py 口径一致): html.unescape 解码实体;
    <br>→\\n; 白名单 HTML 标签删除; \\r\\n|\\r→\\n; \\t→单空格; 删除 \\x7f、其余 C0
    (保留 \\n) 及 C1(0x80-0x9F); 去首尾空白; 递归处理嵌套结构; 不删除任何字段。
 4. 输出保持原有结构, JSON 序列化 ensure_ascii=False。

用法:
   试跑:  python3 cleaning/clean_personal.py --trial
   全量:  python3 cleaning/clean_personal.py
"""

import argparse
import html
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INPUT_DIR = PROJECT_ROOT / "data" / "个人服务"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "cleaned" / "个人服务"
DEFAULT_REJECTS = PROJECT_ROOT / "data" / "rejects" / "个人服务_rejects.jsonl"
DEFAULT_REPORT_JSON = PROJECT_ROOT / "cleaning" / "reports" / "personal_clean_report.json"

TRIAL_OUTPUT_DIR = PROJECT_ROOT / "cleaning" / "trial" / "output"
TRIAL_REJECTS = PROJECT_ROOT / "cleaning" / "trial" / "personal_trial_rejects.jsonl"
TRIAL_REPORT_JSON = PROJECT_ROOT / "cleaning" / "trial" / "personal_trial_report.json"

EXPECTED_SCHEMA_VERSION = "gdzwfw-large-human-readable-v1"
REQUIRED_TOP_KEYS = ("事项", "办理", "办理结果", "常见问答", "来源", "法律依据", "申请")

# ---- 规范化常量 (与法人服务 materialize_legal.py 完全一致) ----
# 控制字符: \t 单独转空格; 删除其余 C0(0x00-0x1F 除 \n)、DEL(\x7f)与 C1(0x80-0x9F)
_CTRL_TABLE = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CTRL_TABLE[0x7F] = None
_CTRL_TABLE.update({c: None for c in range(0x80, 0xA0)})

# 白名单 HTML 标签(只删标签, 不动文本); <br> 先单独转换为换行
_BR_RE = re.compile(r"<br(?:\s[^>]*)?/?>", re.I)
_TAG_RE = re.compile(
    r"</?(?:p|div|span|a|b|strong|em|i|u|ul|ol|li|table|thead|tbody|tfoot|tr|td|th|"
    r"font|h[1-6]|hr|img|sub|sup|section|article|center)(?:\s[^>]*)?/?>", re.I)

# 每条记录清洗时命中的规范化类别(模块级标志, 每记录处理前后重置/读取)
_FLAGS = {"html": False, "tab": False, "ctrl": False}

PROGRESS_EVERY = 100_000


def clean_str(v):
    """html.unescape 解码实体; <br>→\n; 白名单标签删除; \r\n|\r→\n; \t→空格;
    删 \x7f 及其余 C0/C1; 去首尾空白。不改变字段含义。"""
    if "&" in v or "<" in v:
        nv = html.unescape(v)
        nv = _BR_RE.sub("\n", nv)
        nv = _TAG_RE.sub("", nv)
        if nv != v:
            _FLAGS["html"] = True
        v = nv
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    if "\t" in v:
        _FLAGS["tab"] = True
        v = v.replace("\t", " ")
    nv = v.translate(_CTRL_TABLE)
    if nv != v:
        _FLAGS["ctrl"] = True
    return nv.strip()


def normalize(node):
    """递归规范化嵌套结构中的全部字符串; 不删除任何字段。"""
    if isinstance(node, str):
        return clean_str(node)
    if isinstance(node, dict):
        for key, val in node.items():
            node[key] = normalize(val)
        return node
    if isinstance(node, list):
        for idx, val in enumerate(node):
            node[idx] = normalize(val)
        return node
    return node


def is_empty_value(val):
    if val is None:
        return True
    if isinstance(val, str):
        return val == ""
    if isinstance(val, (list, dict)):
        return len(val) == 0
    return False


def walk_field_stats(node, path, total_counter, empty_counter):
    """按点分路径统计字段出现次数与空值次数 (list 元素不展开下标)。"""
    total_counter[path] += 1
    if is_empty_value(node):
        empty_counter[path] += 1
    if isinstance(node, dict):
        for key, val in node.items():
            walk_field_stats(val, f"{path}.{key}", total_counter, empty_counter)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                for key, val in item.items():
                    walk_field_stats(val, f"{path}.{key}", total_counter, empty_counter)


class Cleaner:
    def __init__(self, output_dir, rejects_path):
        self.output_dir = Path(output_dir)
        self.rejects_path = Path(rejects_path)
        self.seen_keys = set()
        self.dup_key_counter = Counter()
        self.reject_reason_counter = Counter()
        self.schema_version_counter = Counter()
        self.extra_top_keys_counter = Counter()
        self.field_total = Counter()
        self.field_empty = Counter()
        self.max_line_bytes = {"bytes": 0, "shard": None, "line_no": 0}
        self.max_legal_content = {"chars": 0, "编码": None, "名称": None, "shard": None}
        self.max_accept_cond = {"chars": 0, "编码": None, "名称": None, "shard": None}
        self.g_input = 0
        self.g_output = 0
        self.g_rejected = 0
        self.g_duplicates = 0
        self.norm_impact = {"html": 0, "tab": 0, "ctrl": 0}
        self.start_time = time.time()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.rejects_path.parent.mkdir(parents=True, exist_ok=True)
        self.rej_fp = open(self.rejects_path, "w", encoding="utf-8", buffering=1024 * 1024)

    def close(self):
        self.rej_fp.close()

    def _write_reject(self, obj_or_raw, reason, shard, line_no, raw_is_text=True):
        rec = {"_reject_reason": reason, "_source_shard": shard, "_source_line": line_no}
        if isinstance(obj_or_raw, dict):
            rec.update(obj_or_raw)
        else:
            rec["_raw_line"] = obj_or_raw if raw_is_text else obj_or_raw.hex()
        self.rej_fp.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _reject(self, obj_or_raw, reason, shard, line_no, raw_is_text=True):
        self.g_rejected += 1
        self.reject_reason_counter[reason] += 1
        self._write_reject(obj_or_raw, reason, shard, line_no, raw_is_text)

    def _duplicate(self, obj, dedup_key, shard, line_no):
        self.g_duplicates += 1
        self.dup_key_counter[dedup_key] += 1
        self._write_reject(obj, "重复记录(去重键已出现, 保留首次)", shard, line_no)

    def _progress(self):
        elapsed = time.time() - self.start_time
        rate = self.g_input / elapsed if elapsed > 0 else 0
        print(
            f"[{time.strftime('%H:%M:%S')}] 已处理 {self.g_input:,} 行 | "
            f"输出 {self.g_output:,} | 拒绝 {self.g_rejected:,} | 重复 {self.g_duplicates:,} | "
            f"耗时 {elapsed:,.0f}s | {rate:,.0f} 行/s",
            flush=True,
        )

    def _track_extremes(self, obj, shard):
        sx = obj.get("事项") or {}
        code = sx.get("编码")
        name = sx.get("名称")
        legal = obj.get("法律依据")
        if isinstance(legal, list):
            for item in legal:
                if isinstance(item, dict):
                    content = item.get("内容")
                    if isinstance(content, str) and len(content) > self.max_legal_content["chars"]:
                        self.max_legal_content = {
                            "chars": len(content), "编码": code, "名称": name, "shard": shard,
                        }
        apply = obj.get("申请")
        if isinstance(apply, dict):
            cond = apply.get("受理条件")
            if isinstance(cond, str) and len(cond) > self.max_accept_cond["chars"]:
                self.max_accept_cond = {
                    "chars": len(cond), "编码": code, "名称": name, "shard": shard,
                }

    def process_shard(self, input_path, max_lines=0):
        shard = input_path.name
        stats = {"shard": shard, "input": 0, "output": 0, "rejected": 0, "duplicates": 0}
        out_path = self.output_dir / shard
        with open(input_path, "rb", buffering=1024 * 1024) as fin, \
                open(out_path, "w", encoding="utf-8", buffering=1024 * 1024) as fout:
            for line_no, raw in enumerate(fin, 1):
                if max_lines and line_no > max_lines:
                    break
                _process_one_line(self, fout, raw, shard, line_no, stats)
        return stats


def build_report(cleaner, shard_stats, duration, mode):
    field_rows = []
    for path, total in cleaner.field_total.items():
        if not path or total < 100:
            continue
        empty = cleaner.field_empty.get(path, 0)
        field_rows.append({
            "path": path, "total": total, "empty": empty,
            "empty_rate": round(empty / total, 6),
        })
    field_rows.sort(key=lambda r: (-r["empty_rate"], -r["total"]))

    dup_top = [
        {"dedup_key": k, "occurrences": v + 1}
        for k, v in cleaner.dup_key_counter.most_common(20)
    ]

    return {
        "dataset": "个人服务",
        "mode": mode,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(duration, 1),
        "global": {
            "input_lines": cleaner.g_input,
            "output_records": cleaner.g_output,
            "rejected": cleaner.g_rejected,
            "duplicates": cleaner.g_duplicates,
            "distinct_dedup_keys": len(cleaner.seen_keys),
            "reject_reasons": dict(cleaner.reject_reason_counter.most_common()),
            "schema_version_counts": {
                str(k): v for k, v in cleaner.schema_version_counter.items()
            },
            "unexpected_top_keys": dict(cleaner.extra_top_keys_counter.most_common()),
            "normalization_impact_records": dict(cleaner.norm_impact),
        },
        "shards": shard_stats,
        "duplicates": {
            "total": cleaner.g_duplicates,
            "distinct_keys": len(cleaner.dup_key_counter),
            "top20_by_occurrences": dup_top,
        },
        "field_empty_rate_top20": field_rows[:20],
        "extremes": {
            "max_line_bytes": cleaner.max_line_bytes,
            "max_legal_basis_content_chars": cleaner.max_legal_content,
            "max_accept_condition_chars": cleaner.max_accept_cond,
        },
    }


def write_markdown(report, md_path):
    g = report["global"]
    lines = [
        f"# 个人服务数据集清洗报告 ({report['mode']})",
        "",
        f"- 生成时间: {report['generated_at']}",
        f"- 耗时: {report['duration_seconds']}s",
        "",
        "## 全局汇总",
        "",
        f"- 输入行数: {g['input_lines']:,}",
        f"- 输出记录: {g['output_records']:,}",
        f"- 拒绝记录: {g['rejected']:,}",
        f"- 重复剔除: {g['duplicates']:,}",
        f"- 去重后唯一键: {g['distinct_dedup_keys']:,}",
        "",
        "### 拒绝原因分布",
        "",
    ]
    if g["reject_reasons"]:
        lines += [f"- {k}: {v:,}" for k, v in g["reject_reasons"].items()]
    else:
        lines.append("- (无)")
    lines += [
        "",
        "### schema_version 分布",
        "",
    ]
    lines += [f"- {k}: {v:,}" for k, v in g["schema_version_counts"].items()] or ["- (无)"]
    if g["unexpected_top_keys"]:
        lines += ["", "### 异常顶层键", ""]
        lines += [f"- {k}: {v:,}" for k, v in g["unexpected_top_keys"].items()]
    impact = g.get("normalization_impact_records")
    if impact:
        lines += [
            "",
            "### 规范化影响记录数 (v2 口径: HTML 实体/标签 + 控制字符)",
            "",
            f"- HTML 实体解码/标签规范化: {impact.get('html', 0):,} 条",
            f"- \\t→空格: {impact.get('tab', 0):,} 条",
            f"- \\x7f/C0/C1 删除: {impact.get('ctrl', 0):,} 条",
        ]
    lines += [
        "",
        "## 各分片统计",
        "",
        "| 分片 | 输入 | 输出 | 拒绝 | 重复 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for s in report["shards"]:
        lines.append(
            f"| {s['shard']} | {s['input']:,} | {s['output']:,} | "
            f"{s['rejected']:,} | {s['duplicates']:,} |"
        )
    dup = report["duplicates"]
    lines += [
        "",
        "## 重复记录",
        "",
        f"- 重复总数: {dup['total']:,}; 涉及唯一键: {dup['distinct_keys']:,}",
        "",
        "### 重复次数 Top 20 编码",
        "",
        "| 去重键 | 出现次数 |",
        "| --- | ---: |",
    ]
    lines += [f"| {d['dedup_key']} | {d['occurrences']} |" for d in dup["top20_by_occurrences"]]
    lines += [
        "",
        "## 字段空值率 Top 20 (出现次数 >= 100)",
        "",
        "| 字段路径 | 出现次数 | 空值次数 | 空值率 |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['path']} | {r['total']:,} | {r['empty']:,} | {r['empty_rate']:.2%} |"
        for r in report["field_empty_rate_top20"]
    ]
    ext = report["extremes"]
    mlb = ext["max_line_bytes"]
    mlc = ext["max_legal_basis_content_chars"]
    mac = ext["max_accept_condition_chars"]
    lines += [
        "",
        "## 极值记录",
        "",
        f"- 最长原始行: {mlb['bytes']:,} 字节 ({mlb['shard']} 第 {mlb['line_no']} 行)",
        f"- 最长法律依据.内容: {mlc['chars']:,} 字符 (编码 {mlc['编码']}, {mlc['shard']})",
        f"- 最长申请.受理条件: {mac['chars']:,} 字符 (编码 {mac['编码']}, {mac['shard']})",
        "",
    ]
    Path(md_path).write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="个人服务数据集清洗")
    parser.add_argument("--trial", action="store_true",
                        help="试跑模式: 仅处理首个分片前 5000 行, 输出到 cleaning/trial/")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rejects-file", default=None)
    parser.add_argument("--report-json", default=None)
    parser.add_argument("--max-lines", type=int, default=0, help="每分片最多处理行数, 0=不限")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if args.trial:
        output_dir = TRIAL_OUTPUT_DIR
        rejects_path = TRIAL_REJECTS
        report_json = TRIAL_REPORT_JSON
        max_lines = 5000
        mode = "trial"
    else:
        output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
        rejects_path = Path(args.rejects_file) if args.rejects_file else DEFAULT_REJECTS
        report_json = Path(args.report_json) if args.report_json else DEFAULT_REPORT_JSON
        max_lines = args.max_lines
        mode = "full"

    shards = sorted(input_dir.glob("*.jsonl"))
    if not shards:
        print(f"错误: 输入目录无 jsonl 分片: {input_dir}", file=sys.stderr)
        sys.exit(1)
    if args.trial:
        shards = shards[:1]

    print(f"模式: {mode} | 输入目录: {input_dir} | 分片数: {len(shards)}", flush=True)
    print(f"输出目录: {output_dir} | rejects: {rejects_path}", flush=True)

    cleaner = Cleaner(output_dir, rejects_path)
    shard_stats = []
    try:
        for shard_path in shards:
            print(f"开始分片 {shard_path.name} ...", flush=True)
            t0 = time.time()
            stats = cleaner.process_shard(shard_path, max_lines=max_lines)
            shard_stats.append(stats)
            print(
                f"分片 {shard_path.name} 完成: 输入 {stats['input']:,} 输出 {stats['output']:,} "
                f"拒绝 {stats['rejected']:,} 重复 {stats['duplicates']:,} "
                f"耗时 {time.time() - t0:,.0f}s",
                flush=True,
            )
    finally:
        cleaner.close()

    duration = time.time() - cleaner.start_time
    report = build_report(cleaner, shard_stats, duration, mode)
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, report_json.with_suffix(".md"))

    g = report["global"]
    print(
        f"全部完成: 输入 {g['input_lines']:,} 输出 {g['output_records']:,} "
        f"拒绝 {g['rejected']:,} 重复 {g['duplicates']:,} 耗时 {duration:,.0f}s",
        flush=True,
    )
    print(f"报告: {report_json} / {report_json.with_suffix('.md')}", flush=True)


def _process_one_line(cleaner, fout, raw, shard, line_no, stats):
    """单行处理逻辑 (供限量模式与正常模式共用)。"""
    stats["input"] += 1
    cleaner.g_input += 1
    if len(raw) > cleaner.max_line_bytes["bytes"]:
        cleaner.max_line_bytes = {"bytes": len(raw), "shard": shard, "line_no": line_no}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        cleaner._reject(raw, "行不是合法 UTF-8", shard, line_no, raw_is_text=False)
        stats["rejected"] += 1
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        cleaner._reject(text, "JSON 解析失败", shard, line_no)
        stats["rejected"] += 1
        return
    if not isinstance(obj, dict):
        cleaner._reject(obj, "顶层不是 JSON 对象", shard, line_no)
        stats["rejected"] += 1
        return
    missing = [k for k in REQUIRED_TOP_KEYS if k not in obj]
    if missing:
        cleaner._reject(obj, f"缺少顶层键: {','.join(missing)}", shard, line_no)
        stats["rejected"] += 1
        return
    sx = obj.get("事项")
    if not isinstance(sx, dict):
        cleaner._reject(obj, "事项 字段不是对象", shard, line_no)
        stats["rejected"] += 1
        return
    raw_code = sx.get("编码")
    raw_name = sx.get("名称")
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not code or not name:
        which = "事项.编码" if not code else "事项.名称"
        if not code and not name:
            which = "事项.编码 与 事项.名称"
        cleaner._reject(obj, f"{which} 为空", shard, line_no)
        stats["rejected"] += 1
        return
    cleaner.schema_version_counter[obj.get("schema_version")] += 1
    for k in obj.keys():
        if k != "schema_version" and k not in REQUIRED_TOP_KEYS:
            cleaner.extra_top_keys_counter[k] += 1
    if code:
        dedup_key = code
    else:
        org = sx.get("实施主体")
        org = org.strip() if isinstance(org, str) else ""
        dedup_key = f"{name}|{org}"
    if dedup_key in cleaner.seen_keys:
        cleaner._duplicate(obj, dedup_key, shard, line_no)
        stats["duplicates"] += 1
        return
    cleaner.seen_keys.add(dedup_key)
    _FLAGS["html"] = _FLAGS["tab"] = _FLAGS["ctrl"] = False
    obj = normalize(obj)
    for _k in cleaner.norm_impact:
        if _FLAGS[_k]:
            cleaner.norm_impact[_k] += 1
    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stats["output"] += 1
    cleaner.g_output += 1
    walk_field_stats(obj, "", cleaner.field_total, cleaner.field_empty)
    cleaner._track_extremes(obj, shard)
    if cleaner.g_input % PROGRESS_EVERY == 0:
        cleaner._progress()


if __name__ == "__main__":
    main()
