#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务事项 JSONL → KAG 结构化构建链输入 CSV 适配器（v0.2：LegalCitation 演进）

读取统一 schema（gdzwfw-large-human-readable-v1）的 JSONL，流式输出
kg/design/schema_design.md 定义的 CSV：

  节点表：Affair / ImplementingOrg / Material / LegalBasis / LegalCitation /
          ProcessStep / ResultDocument / CrossRegionHandling
  概念表：AffairType / ServiceTarget / ExerciseLevel / ThemeCategory（仅 id 列）
  关系表：Affair_requireMaterial_Material / Affair_citeLegal_LegalCitation /
          LegalCitation_partOf_LegalBasis / ProcessStep_nextStep_ProcessStep

格式约定（依据 kg/design/kag_notes.md 研读结论）：
- 首行表头；id 列为主键；语义属性单元格填目标节点 id，多值用英文逗号分隔；
- 空值一律写空字符串——下游需配合 na_filter=False 的 CSVScanner
  （避免 pandas 把空格读成 NaN 写入图）；
- 长文本含换行/逗号由 csv 模块 QUOTE_MINIMAL 转义，pandas 可正确解析。

用法：
  python kg/build/adapter.py \
      --input "data/cleaned/个人服务/*.jsonl" "data/cleaned/法人服务/*.jsonl" \
      --output kg/build/out [--limit 200]

假设（格式要求未尽明确处按 KAG 示例最简可行格式实现）：
- 概念 id 即 isA 层级路径（"-" 连接），由 SPGTypeMapping 自动建链；
- 关系表列固定为 srcId,dstId,<边子属性...>，对应 RelationMapping；
- 同 id 节点重复导入为覆盖语义，适配器按 id 去重（先到先得）。
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys

# ---------------- 概念映射 ----------------

# 事项类型：行政权力十类归为二级概念（行政权力-X），其余一级平铺
_ADMIN_POWER = {
    "行政许可", "行政处罚", "行政强制", "行政征收", "行政给付",
    "行政检查", "行政确认", "行政奖励", "行政裁决", "其他行政权力",
}

# 服务对象：法人类归为二级概念（法人-X），其余一级平铺
_SERVICE_TARGET_MAP = {
    "企业法人": "法人-企业法人",
    "事业法人": "法人-事业法人",
    "社会组织法人": "法人-社会组织法人",
}

_SPLIT_RE = re.compile(r"[,，、;；]+")
_SPLIT_RE_NO_DUN = re.compile(r"[,，;；]+")  # 值内含顿号的字段（如"镇（乡、街道）级"）不按顿号切
_CHINESE_STEM_RE = re.compile(r"^[\u4e00-\u9fa5]")

# ---------------- 表头 ----------------

AFFAIR_COLS = [
    "id", "name",
    "affairType", "serviceTarget", "exerciseLevel", "theme",
    "implementedBy", "hasStep", "produceResult", "supportCrossRegion",
    "status", "officialListCount", "handleDepth", "handleMethods",
    "isOnline", "isCharge", "consultPhone", "complaintPhone",
    "promiseTimeLimit", "promiseTimeNote", "legalTimeLimit", "legalTimeNote",
    "handleAddress", "onlineLimitNote",
    "acceptCondition", "windowProcess", "onlineProcess", "sourceUrl",
]
ORG_COLS = ["id", "name"]
MATERIAL_COLS = ["id", "name"]
LEGAL_COLS = ["id", "title", "docNo"]
CITATION_COLS = ["id", "name", "article", "content"]
STEP_COLS = ["id", "name", "stepIndex", "step", "link", "handler",
             "timeLimit", "result", "reviewStandard"]
RESULT_COLS = ["id", "name", "docType", "validNote", "note", "attachments"]
CROSS_COLS = ["id", "name", "coverRegion", "throughForm", "throughScope"]
REL_MATERIAL_COLS = ["srcId", "dstId", "seq", "copies", "submitForm",
                     "materialType", "materialSource", "isRequired", "note"]
REL_CITE_COLS = ["srcId", "dstId"]
REL_PARTOF_COLS = ["srcId", "dstId"]
REL_NEXT_COLS = ["srcId", "dstId"]
CONCEPT_COLS = ["id"]

NODE_FILES = {
    "Affair": AFFAIR_COLS,
    "ImplementingOrg": ORG_COLS,
    "Material": MATERIAL_COLS,
    "LegalBasis": LEGAL_COLS,
    "LegalCitation": CITATION_COLS,
    "ProcessStep": STEP_COLS,
    "ResultDocument": RESULT_COLS,
    "CrossRegionHandling": CROSS_COLS,
}
REL_FILES = {
    "Affair_requireMaterial_Material": REL_MATERIAL_COLS,
    "Affair_citeLegal_LegalCitation": REL_CITE_COLS,
    "LegalCitation_partOf_LegalBasis": REL_PARTOF_COLS,
    "ProcessStep_nextStep_ProcessStep": REL_NEXT_COLS,
}
CONCEPT_FILES = ["AffairType", "ServiceTarget", "ExerciseLevel", "ThemeCategory"]


# ---------------- 工具函数 ----------------

def norm(s):
    """归一化短文本：去首尾空白、全角空格转半角、压缩连续空白。"""
    if s is None:
        return ""
    s = str(s).replace("　", " ").strip()
    return re.sub(r"\s+", " ", s)


def md5_id(prefix, *parts):
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{h}"


def split_multi(s, allow_dun=True):
    """拆分多值字段，去空白、去空项、保序去重。allow_dun=False 时不按顿号切。"""
    out, seen = [], set()
    for part in (_SPLIT_RE if allow_dun else _SPLIT_RE_NO_DUN).split(s or ""):
        p = norm(part)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def map_affair_type(v):
    return f"行政权力-{v}" if v in _ADMIN_POWER else v


def map_service_target(v):
    return _SERVICE_TARGET_MAP.get(v, v)


# ---------------- 适配器 ----------------

class Adapter:
    def __init__(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        self.output_dir = output_dir
        self._handles = {}
        self._writers = {}
        for name, cols in {**NODE_FILES, **REL_FILES}.items():
            self._open(name, cols)
        # 概念值收集（去重后收尾统一写出）
        self.concepts = {c: [] for c in CONCEPT_FILES}
        self._concept_seen = {c: set() for c in CONCEPT_FILES}
        # 共享节点去重
        self._seen_affair = set()
        self._seen_org = set()
        self._seen_material = set()
        self._seen_legal = set()
        self._seen_citation = set()
        self._citation_content_md5 = {}
        self.stats = {
            "records_read": 0, "affairs_written": 0, "affairs_dup_skipped": 0,
            "parse_errors": 0, "citation_content_conflicts": 0, "rows": {},
        }

    def _open(self, name, cols):
        f = open(os.path.join(self.output_dir, f"{name}.csv"), "w",
                 encoding="utf-8", newline="")
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(cols)
        self._handles[name] = f
        self._writers[name] = w

    def write(self, name, row):
        self._writers[name].writerow(row)
        self.stats["rows"][name] = self.stats["rows"].get(name, 0) + 1

    def add_concept(self, ctype, cid):
        if cid and cid not in self._concept_seen[ctype]:
            self._concept_seen[ctype].add(cid)
            self.concepts[ctype].append(cid)

    def close(self):
        for ctype in CONCEPT_FILES:
            f = open(os.path.join(self.output_dir, f"{ctype}.csv"), "w",
                     encoding="utf-8", newline="")
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            w.writerow(CONCEPT_COLS)
            for cid in self.concepts[ctype]:
                w.writerow([cid])
                self.stats["rows"][ctype] = self.stats["rows"].get(ctype, 0) + 1
            f.close()
        for f in self._handles.values():
            f.close()

    # ---------------- 单条记录处理 ----------------

    def process(self, rec, file_stem):
        s = rec.get("事项") or {}
        b = rec.get("办理") or {}
        apply_ = rec.get("申请") or {}
        src = rec.get("来源") or {}

        code = norm(s.get("编码"))
        name = norm(s.get("名称"))
        org_name = norm(s.get("实施主体"))
        aid = code or md5_id("GA", name, org_name, src.get("详情页URL", ""))
        if aid in self._seen_affair:
            self.stats["affairs_dup_skipped"] += 1
            return False
        self._seen_affair.add(aid)

        # 实施主体（共享节点，id=规范化名称）
        if org_name and org_name not in self._seen_org:
            self._seen_org.add(org_name)
            self.write("ImplementingOrg", [org_name, org_name])

        # 概念：事项类型 / 服务对象 / 行使层级 / 主题分类
        affair_type = map_affair_type(norm(s.get("事项类型")))
        targets = [map_service_target(t) for t in split_multi(s.get("服务对象"))]
        levels = split_multi(s.get("行使层级"), allow_dun=False)
        theme = norm(s.get("主题分类"))
        if not theme and _CHINESE_STEM_RE.match(file_stem):
            # 法人按主题分文件存放，字段缺失时用文件名（主题名）兜底
            theme = file_stem
        if affair_type:
            self.add_concept("AffairType", affair_type)
        for t in targets:
            self.add_concept("ServiceTarget", t)
        for lv in levels:
            self.add_concept("ExerciseLevel", lv)
        if theme:
            self.add_concept("ThemeCategory", theme)

        # 办理环节（弱实体 + nextStep 顺序边）
        step_ids = []
        steps = b.get("办理环节") or []
        prev_sid = None
        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                continue
            sid = f"{aid}#P{i + 1:02d}"
            step_name = norm(st.get("步骤")) or norm(st.get("环节")) or f"环节{i + 1}"
            self.write("ProcessStep", [
                sid, step_name, str(i + 1), norm(st.get("步骤")),
                norm(st.get("环节")), norm(st.get("办理人员")),
                norm(st.get("办理时限")), st.get("办理结果") or "",
                st.get("审查标准") or "",
            ])
            step_ids.append(sid)
            if prev_sid:
                self.write("ProcessStep_nextStep_ProcessStep", [prev_sid, sid])
            prev_sid = sid

        # 办理结果（弱实体）
        result_ids = []
        for i, r in enumerate(rec.get("办理结果") or []):
            if not isinstance(r, dict):
                continue
            rid = f"{aid}#R{i + 1:02d}"
            rname = norm(r.get("名称")) or f"办理结果{i + 1}"
            attachments = json.dumps(r.get("公开附件") or [],
                                     ensure_ascii=False, separators=(",", ":"))
            self.write("ResultDocument", [
                rid, rname, norm(r.get("类型")), r.get("有效期说明") or "",
                r.get("说明") or "", attachments if attachments != "[]" else "",
            ])
            result_ids.append(rid)

        # 跨域通办（弱实体）
        cross_ids = []
        for i, c in enumerate(b.get("跨域通办") or []):
            if not isinstance(c, dict):
                continue
            cid = f"{aid}#C{i + 1:02d}"
            cname = norm(c.get("通办范围")) or norm(c.get("通办形式")) or f"跨域通办{i + 1}"
            self.write("CrossRegionHandling", [
                cid, cname, norm(c.get("覆盖地区")),
                norm(c.get("通办形式")), norm(c.get("通办范围")),
            ])
            cross_ids.append(cid)

        # 申请材料（共享节点 + 关系边属性）
        for m in apply_.get("材料") or []:
            if not isinstance(m, dict):
                continue
            mname = norm(m.get("名称"))
            if not mname:
                continue
            if mname not in self._seen_material:
                self._seen_material.add(mname)
                self.write("Material", [mname, mname])
            self.write("Affair_requireMaterial_Material", [
                aid, mname, norm(m.get("序号")), norm(m.get("份数")),
                norm(m.get("提交形式")), norm(m.get("材料类型")),
                norm(m.get("材料来源")), norm(m.get("是否必要")),
                m.get("说明") or "",
            ])

        # 法律依据（文号共享节点 LegalBasis + 文号|条款 级引用节点 LegalCitation）
        for lb in rec.get("法律依据") or []:
            if not isinstance(lb, dict):
                continue
            doc_no = norm(lb.get("文号"))
            title = norm(lb.get("名称"))
            if not doc_no and not title:
                continue
            lid = doc_no or md5_id("LH", title)
            if lid not in self._seen_legal:
                self._seen_legal.add(lid)
                self.write("LegalBasis", [lid, title, doc_no])
            article = norm(lb.get("条款"))
            content = lb.get("内容") or ""
            cid = md5_id("LC", lid, article)
            if cid not in self._seen_citation:
                self._seen_citation.add(cid)
                self._citation_content_md5[cid] = hashlib.md5(
                    content.encode("utf-8")).hexdigest()
                cname = f"{title} {article}".strip() or lid
                self.write("LegalCitation", [cid, cname, article, content])
                self.write("LegalCitation_partOf_LegalBasis", [cid, lid])
            elif self._citation_content_md5[cid] != hashlib.md5(
                    content.encode("utf-8")).hexdigest():
                # 同一（文号,条款）引用文本仍不一致：保留首见，计数观察
                self.stats["citation_content_conflicts"] += 1
            self.write("Affair_citeLegal_LegalCitation", [aid, cid])

        # 政务事项主表
        count = s.get("官方列表出现次数")
        self.write("Affair", [
            aid, name,
            affair_type, ",".join(targets), ",".join(levels), theme,
            org_name, ",".join(step_ids), ",".join(result_ids),
            ",".join(cross_ids),
            norm(s.get("状态")),
            str(count) if isinstance(count, int) else "",
            norm(b.get("办理深度")), ",".join(b.get("办理方式") or []),
            norm(b.get("可网上办理")), norm(b.get("是否收费")),
            norm(b.get("咨询电话")), norm(b.get("投诉电话")),
            norm(b.get("承诺办结时限")), b.get("承诺时限说明") or "",
            norm(b.get("法定办结时限")), b.get("法定时限说明") or "",
            norm(b.get("办理地址")), b.get("网上办理限制说明") or "",
            apply_.get("受理条件") or "",
            b.get("窗口办理流程") or "", b.get("网上办理流程") or "",
            norm(src.get("详情页URL")),
        ])
        self.stats["affairs_written"] += 1
        return True


def expand_inputs(patterns):
    files = []
    seen = set()
    for p in patterns:
        matched = sorted(glob.glob(p))
        if not matched and os.path.isfile(p):
            matched = [p]
        for fp in matched:
            if fp not in seen:
                seen.add(fp)
                files.append(fp)
    return files


def main():
    ap = argparse.ArgumentParser(description="政务事项 JSONL → KAG 结构化构建 CSV")
    ap.add_argument("--input", nargs="+", required=True,
                    help="输入 JSONL 文件或 glob（可多个）")
    ap.add_argument("--output", required=True, help="CSV 输出目录")
    ap.add_argument("--limit", type=int, default=None,
                    help="最多处理多少条记录（按成功写入的事项计）")
    args = ap.parse_args()

    files = expand_inputs(args.input)
    if not files:
        print("ERROR: 未匹配到任何输入文件", file=sys.stderr)
        sys.exit(1)
    print(f"输入文件 {len(files)} 个，输出目录 {args.output}"
          + (f"，limit={args.limit}" if args.limit else ""))

    adapter = Adapter(args.output)
    try:
        stop = False
        for fp in files:
            if stop:
                break
            stem = os.path.splitext(os.path.basename(fp))[0]
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        adapter.stats["parse_errors"] += 1
                        continue
                    adapter.stats["records_read"] += 1
                    adapter.process(rec, stem)
                    if args.limit and adapter.stats["affairs_written"] >= args.limit:
                        stop = True
                        break
    finally:
        adapter.close()

    stats_path = os.path.join(args.output, "adapter_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(adapter.stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(adapter.stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
