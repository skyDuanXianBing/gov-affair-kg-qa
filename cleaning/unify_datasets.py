#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 个人服务 / 法人服务 两个已清洗数据集合并为统一数据集。

用法：
    python3 cleaning/unify_datasets.py              # 全量合并 + 验证 + 报告
    python3 cleaning/unify_datasets.py --trial 5000 # 小规模试跑（各取 N 行）
    python3 cleaning/unify_datasets.py --selftest   # 合成数据自测跨集去重逻辑
    python3 cleaning/unify_datasets.py --verify-only data/unified

合并规则：
  1. 统一 schema：所有记录 "事项" 区块字段集合一致（个人服务补 "主题分类"，
     置于 "详情返回编码" 之后）；所有记录 "事项" 末尾新增 "数据来源"。
  2. 跨数据集去重：同一 "事项.编码" 同时出现在两侧时，保留整条记录 JSON
     序列化更长的一条；"数据来源" 记为 ["个人服务","法人服务"]；若保留的是
     个人服务记录而法人服务版本的 "主题分类" 有值，则采用法人服务的主题分类。
  3. 输出 data/unified/政务事项-NNNNNN.jsonl，每片 50 万行，个人服务在前。

内存：仅保留法人服务 {编码: [JSON长度, 主题分类, 合并标记]} 字典与去重编码集合，
记录本体全部流式读写。
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONAL_DIR = ROOT / "data" / "cleaned" / "个人服务"
LEGAL_DIR = ROOT / "data" / "cleaned" / "法人服务"
OUT_DIR = ROOT / "data" / "unified"
REPORT_DIR = ROOT / "cleaning" / "reports"
TRIAL_DIR = ROOT / "cleaning" / "trial" / "unified_trial"

SHARD_SIZE = 500_000
SHARD_PREFIX = "政务事项-"
BOTH_SOURCES = ["个人服务", "法人服务"]
# 统一后 "事项" 区块的字段及顺序
EXPECTED_MATTER_KEYS = (
    "事项类型", "名称", "官方列表出现次数", "实施主体", "服务对象",
    "状态", "编码", "行使层级", "详情返回编码", "主题分类", "数据来源",
)


def iter_jsonl(paths, limit=None):
    """流式读取 jsonl 文件列表，yield 解析后的记录。"""
    n = 0
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
                n += 1
                if limit is not None and n >= limit:
                    return


def json_len(rec):
    """整条记录 JSON 序列化长度（跨集去重比较信息量的依据）。"""
    return len(json.dumps(rec, ensure_ascii=False))


def normalize_matter(rec, source, topic_override=None):
    """重建 "事项" 区块：统一字段顺序，补 "主题分类"，末尾追加 "数据来源"。"""
    matter = rec["事项"]
    new = {}
    for k, v in matter.items():
        new[k] = v
        if k == "详情返回编码" and "主题分类" not in matter:
            new["主题分类"] = ""
    if "主题分类" not in new:  # 兜底：原记录缺少 "详情返回编码" 时直接补上
        new["主题分类"] = ""
    if topic_override and not new["主题分类"]:
        new["主题分类"] = topic_override
    new["数据来源"] = source
    rec["事项"] = new
    return rec


class ShardWriter:
    """按固定行数滚动分片的 jsonl 写出器。"""

    def __init__(self, out_dir, shard_size=SHARD_SIZE, prefix=SHARD_PREFIX):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.shard_size = shard_size
        self.prefix = prefix
        self.idx = 0
        self.cur_lines = 0
        self.fh = None
        self.shards = []

    def _open_next(self):
        self.idx += 1
        self.cur_lines = 0
        name = f"{self.prefix}{self.idx:06d}.jsonl"
        self.fh = open(self.out_dir / name, "w", encoding="utf-8")
        self.shards.append({"文件": name, "行数": 0})

    def write(self, rec):
        if self.fh is None or self.cur_lines >= self.shard_size:
            if self.fh is not None:
                self.fh.close()
            self._open_next()
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.cur_lines += 1
        self.shards[-1]["行数"] += 1

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def run(personal_dir=PERSONAL_DIR, legal_dir=LEGAL_DIR, out_dir=OUT_DIR,
        personal_limit=None, legal_limit=None, shard_size=SHARD_SIZE,
        report=True, report_name="unify_report"):
    """执行合并，返回统计信息字典。"""
    t0 = time.time()
    personal_files = sorted(Path(personal_dir).glob("*.jsonl"))
    legal_files = sorted(Path(legal_dir).glob("*.jsonl"))
    if not personal_files or not legal_files:
        raise SystemExit(f"输入目录为空: {personal_dir} / {legal_dir}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = list(out_dir.glob(f"{SHARD_PREFIX}*.jsonl"))
    if stale:
        print(f"[run] 清理输出目录已有分片 {len(stale)} 个: {out_dir}")
        for p in stale:
            p.unlink()

    # ---- Pass A：扫描法人服务，建立 编码 -> [JSON长度, 主题分类, 合并标记]
    # 合并标记: 0=未冲突, 1=跨集重复且个人服务胜, 2=跨集重复且法人服务胜
    print(f"[A] 扫描法人服务 {len(legal_files)} 个文件 ...")
    legal_map = {}
    legal_in = 0
    for rec in iter_jsonl(legal_files, legal_limit):
        legal_in += 1
        m = rec["事项"]
        legal_map[m["编码"]] = [json_len(rec), m.get("主题分类", ""), 0]
    print(f"[A] 法人服务输入 {legal_in} 行，唯一编码 {len(legal_map)} 个")

    # ---- Pass B：流式处理个人服务并写出
    print(f"[B] 处理个人服务 {len(personal_files)} 个文件 ...")
    writer = ShardWriter(out_dir, shard_size=shard_size)
    topic_counter = Counter()
    personal_in = pure_personal = 0
    dup_cross = win_personal = win_legal = 0
    for rec in iter_jsonl(personal_files, personal_limit):
        personal_in += 1
        code = rec["事项"]["编码"]
        entry = legal_map.get(code)
        if entry is None:
            pure_personal += 1
            out = normalize_matter(rec, "个人服务")
        else:
            dup_cross += 1
            if json_len(rec) >= entry[0]:
                entry[2] = 1
                win_personal += 1
                out = normalize_matter(rec, BOTH_SOURCES, topic_override=entry[1])
            else:
                entry[2] = 2  # 法人服务版本胜出，Pass C 中写出
                win_legal += 1
                continue
        topic_counter[out["事项"]["主题分类"]] += 1
        writer.write(out)
    print(f"[B] 个人服务输入 {personal_in} 行；跨集重复 {dup_cross} "
          f"(个人胜 {win_personal} / 法人胜 {win_legal})")

    # ---- Pass C：再次流式扫描法人服务并写出
    print("[C] 处理法人服务 ...")
    pure_legal = 0
    for rec in iter_jsonl(legal_files, legal_limit):
        code = rec["事项"]["编码"]
        entry = legal_map[code]
        if entry[2] == 1:
            continue  # 个人服务版本已胜出并写出
        if entry[2] == 2:
            out = normalize_matter(rec, BOTH_SOURCES)
        else:
            pure_legal += 1
            out = normalize_matter(rec, "法人服务")
        topic_counter[out["事项"]["主题分类"]] += 1
        writer.write(out)
    writer.close()

    total_out = pure_personal + pure_legal + dup_cross
    assert total_out == personal_in + legal_in - dup_cross
    stats = {
        "输入": {"个人服务行数": personal_in, "法人服务行数": legal_in,
                 "合计": personal_in + legal_in},
        "跨集重复编码数": dup_cross,
        "去重裁决": {"个人服务版本胜出": win_personal, "法人服务版本胜出": win_legal},
        "合并后总行数": total_out,
        "数据来源分布": {"纯个人服务": pure_personal, "纯法人服务": pure_legal,
                        "两者兼有": dup_cross},
        "主题分类分布Top20": [
            {"主题分类": k if k else "(空)", "条数": v}
            for k, v in topic_counter.most_common(20)
        ],
        "输出目录": str(out_dir),
        "分片清单": writer.shards,
        "耗时秒": round(time.time() - t0, 1),
    }
    print(f"[run] 合并后总行数 {total_out}，分片 {len(writer.shards)} 个，"
          f"耗时 {stats['耗时秒']}s")

    if report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"{report_name}.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        write_md(stats, REPORT_DIR / f"{report_name}.md", verification=None)
        print(f"[run] 报告已写入 {REPORT_DIR / report_name}.json/.md")
    return stats


def write_md(stats, path, verification):
    """生成 Markdown 统计报告。"""
    lines = [
        "# 数据集归一化合并报告", "",
        "## 输入",
        f"- 个人服务：{stats['输入']['个人服务行数']:,} 行",
        f"- 法人服务：{stats['输入']['法人服务行数']:,} 行",
        f"- 合计：{stats['输入']['合计']:,} 行", "",
        "## 跨集去重",
        f"- 跨集重复编码数：{stats['跨集重复编码数']:,}",
        f"- 保留个人服务版本：{stats['去重裁决']['个人服务版本胜出']:,}",
        f"- 保留法人服务版本：{stats['去重裁决']['法人服务版本胜出']:,}",
        f"- 合并后总行数：{stats['合并后总行数']:,}", "",
        "## 数据来源分布",
        f"- 纯个人服务：{stats['数据来源分布']['纯个人服务']:,}",
        f"- 纯法人服务：{stats['数据来源分布']['纯法人服务']:,}",
        f"- 两者兼有：{stats['数据来源分布']['两者兼有']:,}", "",
        "## 主题分类分布 Top 20", "",
        "| 主题分类 | 条数 |", "| --- | ---: |",
    ]
    for item in stats["主题分类分布Top20"]:
        lines.append(f"| {item['主题分类']} | {item['条数']:,} |")
    lines += ["", "## 分片清单", "", "| 文件 | 行数 |", "| --- | ---: |"]
    for s in stats["分片清单"]:
        lines.append(f"| {s['文件']} | {s['行数']:,} |")
    lines += ["", f"输出目录：`{stats['输出目录']}`，耗时 {stats['耗时秒']}s", ""]
    if verification is not None:
        lines += [
            "## 验证结论", "",
            f"- 重新解析输出总行数：{verification['总行数']:,}"
            f"（与报告一致：{verification['行数一致']}）",
            f"- 编码全局唯一：{verification['编码全局唯一']}"
            f"（重复 {verification['重复编码数']} 个）",
            f"- 事项区块字段结构统一：{verification['键结构统一']}"
            f"（不同结构 {verification['键结构种数']} 种）",
            f"- 数据来源字段合法：{verification['数据来源合法']}"
            f"（非法 {verification['非法数据来源数']} 条）",
            f"- **总体结论：{verification['通过']}**", "",
        ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def verify(out_dir=OUT_DIR, expected_total=None):
    """重新解析全部输出文件，校验行数、编码唯一性、键结构与数据来源合法性。"""
    files = sorted(Path(out_dir).glob(f"{SHARD_PREFIX}*.jsonl"))
    if not files:
        raise SystemExit(f"未找到输出分片: {out_dir}")
    total = dup_codes = bad_source = 0
    codes = set()
    key_structures = Counter()
    t0 = time.time()
    for p in files:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                rec = json.loads(line)
                m = rec["事项"]
                key_structures[tuple(m.keys())] += 1
                code = m["编码"]
                if code in codes:
                    dup_codes += 1
                else:
                    codes.add(code)
                src = m["数据来源"]
                if src not in ("个人服务", "法人服务") and src != BOTH_SOURCES:
                    bad_source += 1
    result = {
        "总行数": total,
        "行数一致": (expected_total is None or total == expected_total),
        "编码全局唯一": dup_codes == 0,
        "重复编码数": dup_codes,
        "键结构统一": set(key_structures) == {EXPECTED_MATTER_KEYS},
        "键结构种数": len(key_structures),
        "数据来源合法": bad_source == 0,
        "非法数据来源数": bad_source,
        "验证耗时秒": round(time.time() - t0, 1),
    }
    result["通过"] = "通过" if (
        result["行数一致"] and result["编码全局唯一"]
        and result["键结构统一"] and result["数据来源合法"]) else "未通过"
    if set(key_structures) != {EXPECTED_MATTER_KEYS}:
        for ks, cnt in key_structures.items():
            if ks != EXPECTED_MATTER_KEYS:
                print(f"[verify] 异常键结构 x{cnt}: {list(ks)}", file=sys.stderr)
    return result


def selftest(tmp_root=TRIAL_DIR / "selftest"):
    """用合成数据验证跨集去重的三条规则（长度裁决 / 数据来源 / 主题分类回填）。"""
    base = {"schema_version": "gdzwfw-large-human-readable-v1",
            "办理": {}, "办理结果": {}, "常见问答": [], "来源": {"采集序号": 1}}

    def personal(code, filler=""):
        rec = dict(base)
        rec["事项"] = {"事项类型": "行政许可", "名称": "事项" + code + filler,
                       "官方列表出现次数": 1, "实施主体": "某厅", "服务对象": "自然人",
                       "状态": "在用", "编码": code, "行使层级": "省级",
                       "详情返回编码": ""}
        return rec

    def legal(code, topic, filler=""):
        rec = dict(base)
        rec["事项"] = {"事项类型": "行政许可", "名称": "事项" + code + filler,
                       "官方列表出现次数": 1, "实施主体": "某厅", "服务对象": "企业法人",
                       "状态": "在用", "编码": code, "行使层级": "省级",
                       "详情返回编码": "", "主题分类": topic}
        return rec

    p_dir = Path(tmp_root) / "in" / "个人服务"
    l_dir = Path(tmp_root) / "in" / "法人服务"
    o_dir = Path(tmp_root) / "out"
    for d in (p_dir, l_dir):
        d.mkdir(parents=True, exist_ok=True)
    # DUP-P：个人服务版本更长 → 个人胜，主题分类回填法人值
    # DUP-L：法人服务版本更长 → 法人胜
    # ONLY-P / ONLY-L：各自独有
    p_dir.joinpath("p.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in [
            personal("ONLY-P"),
            personal("DUP-P", filler="x" * 200),
            personal("DUP-L"),
        ]), encoding="utf-8")
    l_dir.joinpath("l.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in [
            legal("ONLY-L", "交通运输"),
            legal("DUP-P", "医疗卫生"),
            legal("DUP-L", "农林牧渔", filler="y" * 500),
        ]), encoding="utf-8")

    stats = run(personal_dir=p_dir, legal_dir=l_dir, out_dir=o_dir,
                shard_size=1000, report=False)
    checks = []
    ok = stats["跨集重复编码数"] == 2 and stats["合并后总行数"] == 4
    checks.append(("重复数=2 且 总行数=4", ok))
    recs = {json.loads(l)["事项"]["编码"]: json.loads(l)
            for l in (o_dir / f"{SHARD_PREFIX}000001.jsonl")
            .read_text(encoding="utf-8").splitlines()}
    m = recs["DUP-P"]["事项"]
    checks.append(("DUP-P 个人胜且数据来源为列表",
                   m["数据来源"] == BOTH_SOURCES and m["服务对象"] == "自然人"
                   and m["名称"].endswith("x" * 200)))
    checks.append(("DUP-P 主题分类回填法人值", m["主题分类"] == "医疗卫生"))
    m = recs["DUP-L"]["事项"]
    checks.append(("DUP-L 法人胜且保留其字段",
                   m["数据来源"] == BOTH_SOURCES and m["服务对象"] == "企业法人"
                   and m["名称"].endswith("y" * 500)
                   and m["主题分类"] == "农林牧渔"))
    checks.append(("ONLY-P 数据来源/主题分类",
                   recs["ONLY-P"]["事项"]["数据来源"] == "个人服务"
                   and recs["ONLY-P"]["事项"]["主题分类"] == ""))
    checks.append(("ONLY-L 数据来源",
                   recs["ONLY-L"]["事项"]["数据来源"] == "法人服务"))
    v = verify(o_dir, expected_total=4)
    checks.append(("自测输出通过 verify", v["通过"] == "通过"))
    for name, passed in checks:
        print(f"[selftest] {'PASS' if passed else 'FAIL'}: {name}")
    if not all(p for _, p in checks):
        raise SystemExit("selftest 失败")
    print("[selftest] 全部通过")


def main():
    ap = argparse.ArgumentParser(description="合并个人/法人服务数据集为统一数据集")
    ap.add_argument("--trial", type=int, metavar="N",
                    help="试跑模式：两侧各取前 N 行，输出到 cleaning/trial/unified_trial")
    ap.add_argument("--selftest", action="store_true", help="合成数据自测去重逻辑")
    ap.add_argument("--verify-only", metavar="DIR", help="仅验证指定输出目录")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.verify_only:
        r = verify(args.verify_only)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if args.trial:
        stats = run(out_dir=TRIAL_DIR, personal_limit=args.trial,
                    legal_limit=args.trial, shard_size=3000,
                    report_name="unify_trial_report")
        v = verify(TRIAL_DIR, expected_total=stats["合并后总行数"])
        print(f"[trial] 验证: {json.dumps(v, ensure_ascii=False)}")
        return

    # 全量：合并 → 验证 → 把验证结论写回报告
    stats = run()
    v = verify(OUT_DIR, expected_total=stats["合并后总行数"])
    stats["验证"] = v
    (REPORT_DIR / "unify_report.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(stats, REPORT_DIR / "unify_report.md", verification=v)
    print(f"[verify] {json.dumps(v, ensure_ascii=False)}")
    print(f"[done] 报告已更新: {REPORT_DIR}/unify_report.json 与 unify_report.md")
    if v["通过"] != "通过":
        raise SystemExit("验证未通过")


if __name__ == "__main__":
    main()
