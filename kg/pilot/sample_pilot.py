#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
试点数据分层抽样：从 data/unified/（或 data/cleaned/）抽 10,000 条到 pilot JSONL。

策略（random seed 固定 42）：
- 个人服务 5,000 条 + 法人服务 5,000 条（按 来源.运行ID 中 personal/legal 标记区分）；
- 法人侧按主题分类分层：比例分配 + 每主题最低 10 条，覆盖全部主题（≥10 个）；
- 每层内优先抽信息完整度高的记录（有材料+有法条+有办理环节 = 3 分，
  按得分分组，组内用 seed=42 打乱，依次取满配额）；
- 按事项编码去重（同一编码多条记录只保留首次出现）；
- 两遍流式扫描：第一遍只记元数据决定抽样集合，第二遍按集合回写原始行，
  避免 31GB 数据驻留内存。

输出：
- kg/pilot/pilot_10000.jsonl   抽样原始记录
- kg/pilot/pilot_manifest.json 抽样清单（来源文件、编码、主题分布统计）

用法：
  python kg/pilot/sample_pilot.py \
      [--input "data/unified/*.jsonl"] [--output kg/pilot] \
      [--personal 5000] [--legal 5000] [--seed 42] [--min-per-theme 10]
"""

import argparse
import collections
import glob
import json
import os
import sys
import time


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def iter_records(files, want_full=False):
    """流式产出 (shard_path, line_no, raw_line, record)。"""
    for fp in files:
        with open(fp, "rb") as f:
            for line_no, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield fp, line_no, raw, rec


def record_meta(rec):
    """提取抽样所需元数据。"""
    s = rec.get("事项") or {}
    b = rec.get("办理") or {}
    a = rec.get("申请") or {}
    src = rec.get("来源") or {}
    run_id = src.get("运行ID") or ""
    if "personal" in run_id:
        cat = "个人服务"
    elif "legal" in run_id:
        cat = "法人服务"
    else:
        cat = "未知"
    score = (
        bool(b.get("办理环节"))
        + bool(a.get("材料"))
        + bool(rec.get("法律依据"))
    )
    return {
        "code": (s.get("编码") or "").strip(),
        "cat": cat,
        "theme": (s.get("主题分类") or "").strip(),
        "score": score,
        "run_id": run_id,
        "src_detail": src.get("官方详情JSON") or "",
    }


def stratified_select(candidates, quota, rng):
    """按 score 分组优先抽高分，组内 seed 打乱，取满 quota。"""
    by_score = collections.defaultdict(list)
    for c in candidates:
        by_score[c["score"]].append(c)
    picked = []
    for score in (3, 2, 1, 0):
        group = by_score.get(score, [])
        rng.shuffle(group)
        picked.extend(group)
        if len(picked) >= quota:
            break
    return picked[:quota]


def allocate_legal_quotas(theme_counts, total_quota, min_per_theme):
    """法人侧按主题比例分配 + 每主题最低 min_per_theme，总和=total_quota。"""
    themes = sorted(theme_counts)
    total = sum(theme_counts.values())
    quotas = {}
    for t in themes:
        q = max(min_per_theme, round(total_quota * theme_counts[t] / total))
        quotas[t] = min(q, theme_counts[t])
    # 总和超出则按比例从大到小回收；不足则向有余量的主题（按规模降序）补足
    def current():
        return sum(quotas.values())
    while current() > total_quota:
        for t in sorted(themes, key=lambda x: -quotas[x]):
            if current() <= total_quota:
                break
            if quotas[t] > min_per_theme:
                quotas[t] -= 1
    while current() < total_quota:
        progressed = False
        for t in sorted(themes, key=lambda x: -theme_counts[x]):
            if current() >= total_quota:
                break
            if quotas[t] < theme_counts[t]:
                quotas[t] += 1
                progressed = True
        if not progressed:
            break
    return quotas


def main():
    ap = argparse.ArgumentParser(description="试点 10,000 条分层抽样（seed=42）")
    ap.add_argument("--input", nargs="+", default=["data/unified/*.jsonl"],
                    help="输入 JSONL glob（可多个），默认 data/unified/*.jsonl")
    ap.add_argument("--output", default="kg/pilot", help="输出目录")
    ap.add_argument("--personal", type=int, default=5000)
    ap.add_argument("--legal", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-per-theme", type=int, default=10)
    args = ap.parse_args()

    files = []
    for p in args.input:
        files.extend(sorted(glob.glob(p)))
    if not files:
        log("ERROR: 未匹配到输入文件")
        sys.exit(1)
    log(f"输入文件 {len(files)} 个: {files}")

    # ---------------- 第一遍：收集元数据（按编码去重，先到先得） ----------------
    seen_codes = set()
    personal, legal = [], collections.defaultdict(list)  # theme -> [meta]
    n_read = 0
    t0 = time.time()
    for fp, line_no, raw, rec in iter_records(files):
        n_read += 1
        if n_read % 200000 == 0:
            log(f"pass1 已读 {n_read} 行（{time.time() - t0:.0f}s）")
        m = record_meta(rec)
        if not m["code"] or m["code"] in seen_codes:
            continue
        m["shard"] = fp
        m["line_no"] = line_no
        if m["cat"] == "个人服务":
            seen_codes.add(m["code"])
            personal.append(m)
        elif m["cat"] == "法人服务":
            seen_codes.add(m["code"])
            legal[m["theme"] or "<无主题>"].append(m)
    log(f"pass1 完成：共 {n_read} 行，个人候选 {len(personal)}，"
        f"法人候选 {sum(len(v) for v in legal.values())}（{len(legal)} 个主题），"
        f"耗时 {time.time() - t0:.0f}s")

    # ---------------- 抽样 ----------------
    import random
    rng = random.Random(args.seed)

    picked_personal = stratified_select(personal, args.personal, rng)
    theme_counts = {t: len(v) for t, v in legal.items()}
    quotas = allocate_legal_quotas(theme_counts, args.legal, args.min_per_theme)
    picked_legal = []
    for t in sorted(legal):
        group = stratified_select(legal[t], quotas[t], rng)
        for m in group:
            m["quota"] = quotas[t]
        picked_legal.extend(group)
    log(f"抽样结果：个人 {len(picked_personal)}，法人 {len(picked_legal)}"
        f"（覆盖 {sum(1 for t in quotas if quotas[t] > 0)} 个主题）")

    selected = {}
    for m in picked_personal + picked_legal:
        selected[m["code"]] = m

    # ---------------- 第二遍：回写被选中的原始行 ----------------
    out_jsonl = os.path.join(args.output, "pilot_10000.jsonl")
    os.makedirs(args.output, exist_ok=True)
    n_written, n2 = 0, 0
    emitted = set()
    t1 = time.time()
    with open(out_jsonl, "w", encoding="utf-8") as out:
        for fp, line_no, raw, rec in iter_records(files, want_full=True):
            n2 += 1
            if n2 % 200000 == 0:
                log(f"pass2 已读 {n2} 行（{time.time() - t1:.0f}s）")
            code = ((rec.get("事项") or {}).get("编码") or "").strip()
            if code and code in selected and code not in emitted:
                emitted.add(code)
                out.write(raw.decode("utf-8") + "\n")
                n_written += 1
    log(f"pass2 完成：写出 {n_written} 行到 {out_jsonl}，耗时 {time.time() - t1:.0f}s")

    # ---------------- 抽样清单 ----------------
    def score_dist(metas):
        c = collections.Counter(m["score"] for m in metas)
        return {str(k): c.get(k, 0) for k in (3, 2, 1, 0)}

    theme_stats = {}
    for t in sorted(legal):
        sel = [m for m in picked_legal if m["theme"] == t]
        theme_stats[t] = {
            "total": theme_counts[t],
            "selected": len(sel),
            "quota": quotas[t],
            "selected_score_dist": score_dist(sel),
        }
    manifest = {
        "seed": args.seed,
        "generated_by": "kg/pilot/sample_pilot.py",
        "source_files": files,
        "params": {"personal": args.personal, "legal": args.legal,
                   "min_per_theme": args.min_per_theme},
        "scan": {"lines_read": n_read,
                 "personal_candidates": len(personal),
                 "legal_candidates": sum(theme_counts.values()),
                 "legal_theme_count": len(legal)},
        "result": {
            "written": n_written,
            "personal_selected": len(picked_personal),
            "personal_score_dist": score_dist(picked_personal),
            "legal_selected": len(picked_legal),
            "legal_theme_stats": theme_stats,
            "legal_score_dist": score_dist(picked_legal),
        },
        "records": [
            {"code": m["code"], "cat": m["cat"], "theme": m["theme"],
             "score": m["score"], "shard": m["shard"], "line_no": m["line_no"],
             "src_detail": m["src_detail"]}
            for m in picked_personal + picked_legal
        ],
    }
    manifest_path = os.path.join(args.output, "pilot_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log(f"清单写出：{manifest_path}")
    print(json.dumps({k: manifest[k] for k in ("seed", "scan", "result")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
