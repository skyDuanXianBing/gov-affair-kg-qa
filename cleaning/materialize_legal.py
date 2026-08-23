#!/usr/bin/env python3
"""法人服务 JSONL 物化 + 清洗:原始行 → gdzwfw-large-human-readable-v1。

流式逐行处理;两遍扫描:第一遍物化+去重+计数,第二遍回填"官方列表出现次数"
并重算 结构化记录_SHA256。仅标准库。

v2 修订(质量审计修复):
1. HTML 规范化:html.unescape 解码实体;<br>→换行;白名单 HTML 标签删除
2. 控制字符:\\t→单空格;删 \\x7f 及其余 C0/C1(保留 \\n)
3. v2 分支:special_item_type="TC" 套餐服务(小写键 schema)物化到同一 v1 格式
4. ITEM_ID 缺失时按 TASK_CODE→ROWGUID→NATION_TASK_CODE 兜底编码

用法:
  python3 cleaning/materialize_legal.py                      # 全量 49 文件
  python3 cleaning/materialize_legal.py --files 交通运输.jsonl --limit 5000 \
      --outdir cleaning/test_output/法人服务 \
      --rejects cleaning/test_output/rejects.jsonl \
      --report-prefix cleaning/test_output/report
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import Counter

SCHEMA_VERSION = "gdzwfw-large-human-readable-v1"
RUN_ID = "materialize-legal-2026-08"
BASE_DIR = "/Volumes/f/AllMyData/MyUnderGraduate/政务大模型"
SRC_DIR = os.path.join(BASE_DIR, "data", "法人服务")

# 控制字符:\t 单独转空格;删除其余 C0(0x00-0x1F 除 \n)、DEL(\x7f)与 C1(0x80-0x9F)
_CTRL_TABLE = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CTRL_TABLE[0x7F] = None
_CTRL_TABLE.update({c: None for c in range(0x80, 0xA0)})

# 白名单 HTML 标签(只删标签,不动文本);<br> 先单独转换为换行
_BR_RE = re.compile(r"<br(?:\s[^>]*)?/?>", re.I)
_TAG_RE = re.compile(
    r"</?(?:p|div|span|a|b|strong|em|i|u|ul|ol|li|table|thead|tbody|tfoot|tr|td|th|"
    r"font|h[1-6]|hr|img|sub|sup|section|article|center)(?:\s[^>]*)?/?>", re.I)

# 每条记录清洗时命中的规范化类别(模块级标志,finalize 重置/读取)
_FLAGS = {"html": False, "tab": False, "ctrl": False}


def clean_str(v):
    """HTML 实体解码;<br>→\n;白名单标签删除;\r\n|\r→\n;\t→空格;删 \x7f 及 C0/C1;去首尾空白。"""
    if not isinstance(v, str):
        return v
    if "&" in v or "<" in v:
        pv = v
        for _ in range(3):  # 数据存在双重编码(&amp;amp;),迭代解码至稳定(至多3轮)
            nv = html.unescape(pv)
            if nv == pv:
                break
            pv = nv
        nv = _BR_RE.sub("\n", pv)
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


def deep_clean(node):
    if isinstance(node, str):
        return clean_str(node)
    if isinstance(node, list):
        return [deep_clean(x) for x in node]
    if isinstance(node, dict):
        return {k: deep_clean(v) for k, v in node.items()}
    return node


def s(v):
    """标量取值→字符串;None/缺失→'';bool→是/否;数字→str。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def sha256_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def attach_list(items):
    """v1 附件 list → [{名称, 公开链接}]"""
    out = []
    if isinstance(items, list):
        for a in items:
            if isinstance(a, dict):
                out.append({"名称": s(a.get("ATTACHNAME")), "公开链接": s(a.get("FILEPATH"))})
    return out


def attach_list_v2(items):
    """v2 附件 list → [{名称, 公开链接}];公开链接取外网 waiwang_url。"""
    out = []
    if isinstance(items, list):
        for a in items:
            if isinstance(a, dict):
                out.append({"名称": s(a.get("name")) or s(a.get("original_name")),
                            "公开链接": s(a.get("waiwang_url"))})
    return out


def day_text(day, unit):
    d = s(day)
    if not d:
        return ""
    u = s(unit)
    return f"{d}个{u}" if u and not d.endswith(u) else d


def _base_record(theme, raw_sha, line_no):
    return {
        "schema_version": SCHEMA_VERSION,
        "事项": {"事项类型": "", "名称": "", "官方列表出现次数": 1, "实施主体": "", "服务对象": "",
                "状态": "", "编码": "", "行使层级": "", "详情返回编码": "", "主题分类": theme},
        "办理": {"办理地址": "", "办理方式": [], "办理深度": "", "办理环节": [], "可网上办理": "",
                "咨询电话": "", "承诺办结时限": "", "承诺时限说明": "", "投诉电话": "", "是否收费": "",
                "法定办结时限": "", "法定时限说明": "", "窗口办理流程": "", "网上办理流程": "",
                "网上办理限制说明": "", "跨域通办": []},
        "办理结果": [],
        "常见问答": [],
        "申请": {"受理条件": "", "材料": []},
        "法律依据": [],
        "来源": {"官方详情JSON": "", "官方详情JSON_SHA256": raw_sha, "结构化记录_SHA256": "",
                "详情页URL": "", "运行ID": RUN_ID, "采集序号": line_no},
    }


def materialize_v1(obj, theme, raw_sha, line_no):
    """AUDIT_ITEM 大写键 schema → v1 记录。返回 (record, code, used_fallback)。"""
    item = obj.get("AUDIT_ITEM")
    if isinstance(item, list):
        item = item[0] if item else None
    ext = obj.get("AUDIT_ITEM_EXTEND")
    if not isinstance(ext, dict):
        ext = {}

    code = s(item.get("ITEM_ID"))
    used_fallback = False
    if not code:  # 兜底:TASK_CODE → ROWGUID → NATION_TASK_CODE
        for fb in ("TASK_CODE", "ROWGUID", "NATION_TASK_CODE"):
            code = s(item.get(fb))
            if code:
                used_fallback = True
                break
    name = s(item.get("CATANAME"))

    addr = s(item.get("XHTSDZ"))
    if not addr:
        lobby = obj.get("AUDIT_CATALOG_LOBBY")
        if isinstance(lobby, list) and lobby and isinstance(lobby[0], dict):
            addr = s(lobby[0].get("ADDRESS"))

    ht = s(item.get("HANDLE_TYPE_TEXT"))
    modes = [m for m in (x.strip() for x in ht.split(",")) if m] if ht else []

    flows = []
    fs = obj.get("AUDIT_ITEM_FLOWSHEET")
    if isinstance(fs, list):
        for r in fs:
            if isinstance(r, dict):
                flows.append({
                    "办理人员": s(r.get("TRANSACTOR")),
                    "办理时限": s(r.get("TRANSACT_TIME_LIMIT")),
                    "办理结果": s(r.get("TRANSACT_RESULT")),
                    "审查标准": s(r.get("CHECK_STANDARD")),
                    "步骤": s(r.get("STEP_TEXT")),
                    "环节": s(r.get("UNTI_LINK_TEXT")),
                })

    spans = []
    scopes = item.get("SCOPES")
    if isinstance(scopes, list):
        for sc in scopes:
            if not isinstance(sc, dict):
                continue
            divs, seen_d = [], set()
            for d in sc.get("DIVISIONS") or []:
                if isinstance(d, dict):
                    nm = s(d.get("DIVISION_NAME"))
                    if nm and nm not in seen_d:
                        seen_d.add(nm)
                        divs.append(nm)
            spans.append({
                "覆盖地区": "、".join(divs),
                "通办形式": s(sc.get("SCOPESHAPE_TEXT")),
                "通办范围": s(sc.get("SCOPERANGE_TEXT")),
            })

    unonline = s(ext.get("UNONLINEREASON")) or s(item.get("UNONLINEREASONOTHER"))

    results = []
    rs = obj.get("AUDIT_ITEM_RESULT")
    if isinstance(rs, list):
        for r in rs:
            if isinstance(r, dict):
                results.append({
                    "名称": s(r.get("NAME")),
                    "类型": s(r.get("RESULT_TYPE_TEXT")) or s(r.get("SUBJECT_RESULT_TYPE_TEXT")),
                    "说明": s(r.get("RESUL_EXPLAIN")),
                    "有效期说明": s(r.get("CARD_VALIDDATE")),
                    "公开附件": attach_list(r.get("RESULTATTACHLIST")),
                })

    qas = []
    qa = obj.get("AUDIT_QA")
    if isinstance(qa, list):
        for r in qa:
            if isinstance(r, dict):
                qas.append({"问题": s(r.get("QUESTION")), "答复": s(r.get("ANSWER"))})

    mats = []
    ms = obj.get("AUDIT_MATERIAL")
    if isinstance(ms, list):
        for r in ms:
            if not isinstance(r, dict):
                continue
            note = s(r.get("MATERIAL_DESC")) or s(r.get("FILL_EXPLIAN"))
            attach = attach_list(r.get("FORM_GUID")) + attach_list(r.get("EXAMPLE_GUID"))
            mats.append({
                "序号": s(r.get("ORDERNUM")),
                "名称": s(r.get("MATERIAL_NAME")),
                "份数": s(r.get("PAGE_NUM")),
                "页数": "",
                "提交形式": s(r.get("ZZHDZB_TEXT")),
                "材料类型": s(r.get("MATERIAL_TYPE_TEXT")),
                "材料来源": s(r.get("SOURCE_TYPE_TEXT")),
                "是否必要": s(r.get("IS_NEED_TEXT")),
                "说明": note,
                "公开附件": attach,
            })

    laws = []
    law = item.get("LAW")
    if isinstance(law, list):
        for r in law:
            if isinstance(r, dict):
                laws.append({
                    "名称": s(r.get("LAWNAME")),
                    "文号": s(r.get("ACCORDINGNUMBER")),
                    "条款": s(r.get("TERMSNUMBER")),
                    "内容": s(r.get("TERMSCONTENT")),
                })

    rec = _base_record(theme, raw_sha, line_no)
    rec["事项"].update({
        "事项类型": s(item.get("TASK_TYPE_TEXT")),
        "名称": name,
        "实施主体": s(item.get("DEPT_NAME")),
        "服务对象": s(item.get("SERVE_TYPE_TEXT")),
        "状态": s(item.get("TASK_STATE_TEXT")),
        "编码": code,
        "行使层级": s(item.get("USE_LEVEL_TEXT")),
        "详情返回编码": s(obj.get("errCode")),
    })
    rec["办理"].update({
        "办理地址": addr,
        "办理方式": modes,
        "办理深度": s(item.get("WBSD_LEVEL_TEXT")),
        "办理环节": flows,
        "可网上办理": s(item.get("ONLINECHECK_TEXT")),
        "咨询电话": s(item.get("LINK_TEL")),
        "承诺办结时限": day_text(item.get("PROMISE_DAY"), item.get("PROMISE_TYPE_TEXT")),
        "承诺时限说明": s(item.get("CRBJSXSM")),
        "投诉电话": s(item.get("TSTEL")),
        "是否收费": s(item.get("IS_FEE_TEXT")),
        "法定办结时限": day_text(item.get("ANTICIPATE_DAY"), item.get("ANTICIPATE_TYPE_TEXT")),
        "法定时限说明": s(item.get("FDBLSXSM")),
        "窗口办理流程": s(item.get("CKBLLC")),
        "网上办理流程": s(item.get("WSBLLC")),
        "网上办理限制说明": unonline,
        "跨域通办": spans,
    })
    rec["办理结果"] = results
    rec["常见问答"] = qas
    rec["申请"] = {"受理条件": s(item.get("ACCEPT_CONDITION")), "材料": mats}
    rec["法律依据"] = laws
    return rec, code, used_fallback


def materialize_v2(obj, theme, raw_sha, line_no):
    """special_item_type="TC" 套餐服务(小写键 v2 schema)→ 同一 v1 记录。"""
    code = s(obj.get("id"))
    name = s(obj.get("catalog_name")) or s(obj.get("subject_name"))

    addr = ""
    wl = obj.get("window_list")
    if isinstance(wl, list) and wl and isinstance(wl[0], dict):
        addr = s(wl[0].get("address"))

    ch = s(obj.get("application_channel_text"))
    modes = [m for m in (x.strip() for x in ch.split(",")) if m] if ch else []
    online = ("是" if "网上办理" in ch else "否") if ch else ""

    spans = []
    for sc in obj.get("application_scope_v2_list") or []:
        if not isinstance(sc, dict):
            continue
        divs, seen_d = [], set()
        for d in sc.get("division_list") or []:
            if isinstance(d, dict):
                nm = s(d.get("division_name"))
                if nm and nm not in seen_d:
                    seen_d.add(nm)
                    divs.append(nm)
        spans.append({
            "覆盖地区": "、".join(divs),
            "通办形式": s(sc.get("application_shape_text")),
            "通办范围": s(sc.get("application_scope_v2_text")),
        })

    results = []
    for r in obj.get("result_sample_list") or []:
        if isinstance(r, dict):
            results.append({
                "名称": s(r.get("name")),
                "类型": s(r.get("subject_result_type_text")),
                "说明": "",
                "有效期说明": "",
                "公开附件": attach_list_v2(r.get("sample_att_list")),
            })

    qas = []
    for r in obj.get("question_list") or []:
        if isinstance(r, dict):
            qas.append({"问题": s(r.get("question")), "答复": s(r.get("answer"))})

    mats = []
    for r in obj.get("submit_material_list") or []:
        if not isinstance(r, dict):
            continue
        attach = attach_list_v2(r.get("blank_att_list")) + attach_list_v2(r.get("sample_att_list"))
        mats.append({
            "序号": s(r.get("sort_order")),
            "名称": s(r.get("document_name")),
            "份数": s(r.get("origin_count")),
            "页数": "",
            "提交形式": s(r.get("document_media_text")),
            "材料类型": s(r.get("material_type_text")),
            "材料来源": s(r.get("document_source_text")),
            "是否必要": s(r.get("document_need_type_text")),
            "说明": s(r.get("note")),
            "公开附件": attach,
        })

    laws = []
    for r in obj.get("clause_list") or []:
        if isinstance(r, dict):
            laws.append({
                "名称": s(r.get("legal_name")),
                "文号": s(r.get("reference_number")),
                "条款": s(r.get("clause_number")),
                "内容": s(r.get("content")),
            })

    rec = _base_record(theme, raw_sha, line_no)
    rec["事项"].update({
        "事项类型": s(obj.get("special_item_type_text")),
        "名称": name,
        "实施主体": s(obj.get("impl_org_name")),
        "服务对象": s(obj.get("service_object_text")),
        "状态": s(obj.get("status_text")),
        "编码": code,
        "行使层级": s(obj.get("authority_level_text")),
    })
    rec["办理"].update({
        "办理地址": addr,
        "办理方式": modes,
        "办理深度": s(obj.get("online_apply_depth_text")),
        "可网上办理": online,
        "咨询电话": s(obj.get("consult_phone")),
        "承诺办结时限": day_text(obj.get("promise_time"), obj.get("promise_time_unit_text")),
        "承诺时限说明": s(obj.get("promise_time_note")),
        "投诉电话": s(obj.get("complaint_phone")),
        "是否收费": s(obj.get("is_charge_text")),
        "法定办结时限": day_text(obj.get("legal_time"), obj.get("legal_time_unit_text")),
        "法定时限说明": s(obj.get("legal_time_note")),
        "窗口办理流程": s(obj.get("window_flow")),
        "网上办理流程": s(obj.get("online_flow")),
        "跨域通办": spans,
    })
    rec["办理结果"] = results
    rec["常见问答"] = qas
    rec["申请"] = {"受理条件": s(obj.get("conditions")), "材料": mats}
    rec["法律依据"] = laws
    return rec, code, False


def materialize(obj, theme, raw_sha, line_no):
    """原始 obj → v1 记录。返回 (record, code, kind, used_fallback);record=None 表示无事项内容。"""
    item = obj.get("AUDIT_ITEM")
    if isinstance(item, list):
        item = item[0] if item else None
    if isinstance(item, dict) and item:
        rec, code, fb = materialize_v1(obj, theme, raw_sha, line_no)
        return rec, code, "v1", fb
    if "catalog_name" in obj or obj.get("special_item_type") == "TC":
        rec, code, fb = materialize_v2(obj, theme, raw_sha, line_no)
        return rec, code, "v2", fb
    return None, "", "", False


def finalize(rec):
    """清洗后重算 结构化记录_SHA256 并返回 (序列化行, 命中标志)。"""
    _FLAGS["html"] = _FLAGS["tab"] = _FLAGS["ctrl"] = False
    rec = deep_clean(rec)
    flags = dict(_FLAGS)
    rec["来源"]["结构化记录_SHA256"] = ""
    h = hashlib.sha256(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    rec["来源"]["结构化记录_SHA256"] = h
    return json.dumps(rec, ensure_ascii=False), flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", default="", help="逗号分隔文件名;默认全部 49 个")
    ap.add_argument("--limit", type=int, default=0, help="每文件最多处理行数(0=全部)")
    ap.add_argument("--outdir", default=os.path.join(BASE_DIR, "data", "cleaned", "法人服务"))
    ap.add_argument("--rejects", default=os.path.join(BASE_DIR, "data", "cleaned", "rejects", "法人服务_rejects.jsonl"))
    ap.add_argument("--report-prefix", default=os.path.join(BASE_DIR, "cleaning", "reports", "legal_materialize_report"))
    args = ap.parse_args()

    if args.files:
        files = [f for f in args.files.split(",") if f]
    else:
        files = sorted(f for f in os.listdir(SRC_DIR) if f.endswith(".jsonl"))
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(os.path.dirname(args.rejects), exist_ok=True)
    os.makedirs(os.path.dirname(args.report_prefix), exist_ok=True)

    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    t0 = time.time()
    counts = Counter()          # 编码 -> 出现次数(全部行,含重复)
    id_themes = {}              # 编码 -> set(themes)
    seen = set()                # 已输出编码
    per_file = {}
    rej_f = open(args.rejects, "w", encoding="utf-8")

    for fname in files:
        theme = fname[:-len(".jsonl")]
        fin = os.path.join(SRC_DIR, fname)
        fout = os.path.join(args.outdir, fname)
        st = {"input": 0, "output": 0, "duplicates": 0, "v2_rescued": 0, "fallback_rescued": 0,
              "html_normalized_rows": 0, "tab_normalized_rows": 0,
              "rejects": {"bad_json": 0, "no_audit_item": 0, "missing_code_or_name": 0}}
        out_f = open(fout, "w", encoding="utf-8")
        with open(fin, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                if args.limit and ln > args.limit:
                    break
                st["input"] += 1
                raw = line.strip()
                if not raw:
                    st["rejects"]["bad_json"] += 1
                    rej_f.write(json.dumps({"_raw": "", "_reject_reason": "bad_json",
                                            "_source_file": fname, "_source_line": ln},
                                           ensure_ascii=False) + "\n")
                    continue
                raw_sha = sha256_text(raw)
                try:
                    obj = json.loads(raw)
                except Exception:
                    st["rejects"]["bad_json"] += 1
                    rej_f.write(json.dumps({"_raw": raw, "_reject_reason": "bad_json",
                                            "_source_file": fname, "_source_line": ln},
                                           ensure_ascii=False) + "\n")
                    continue
                rec, code, kind, used_fb = materialize(obj, theme, raw_sha, ln)
                if rec is None:
                    st["rejects"]["no_audit_item"] += 1
                    obj["_reject_reason"] = "no_audit_item"
                    obj["_source_file"] = fname
                    obj["_source_line"] = ln
                    rej_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue
                counts[code] += 1
                id_themes.setdefault(code, set()).add(theme)
                if not code or not rec["事项"]["名称"]:
                    st["rejects"]["missing_code_or_name"] += 1
                    obj["_reject_reason"] = "missing_code_or_name"
                    obj["_source_file"] = fname
                    obj["_source_line"] = ln
                    rej_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue
                if code in seen:
                    st["duplicates"] += 1
                    continue
                seen.add(code)
                st["output"] += 1
                if kind == "v2":
                    st["v2_rescued"] += 1
                if used_fb:
                    st["fallback_rescued"] += 1
                line_out, flags = finalize(rec)
                if flags["html"]:
                    st["html_normalized_rows"] += 1
                if flags["tab"]:
                    st["tab_normalized_rows"] += 1
                out_f.write(line_out + "\n")
        out_f.close()
        per_file[fname] = st
        print(f"[{time.strftime('%H:%M:%S')}] {fname}: input={st['input']} output={st['output']} "
              f"dup={st['duplicates']} rej={sum(st['rejects'].values())} v2={st['v2_rescued']} "
              f"fb={st['fallback_rescued']} html={st['html_normalized_rows']}", flush=True)

    rej_f.close()

    # 第二遍:回填 官方列表出现次数 并重算 hash
    for fname in files:
        fout = os.path.join(args.outdir, fname)
        tmp = fout + ".tmp"
        with open(fout, "r", encoding="utf-8") as fi, open(tmp, "w", encoding="utf-8") as fo:
            for line in fi:
                rec = json.loads(line)
                c = rec["事项"]["编码"]
                rec["事项"]["官方列表出现次数"] = counts.get(c, 1)
                rec["来源"]["结构化记录_SHA256"] = ""
                h = hashlib.sha256(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                rec["来源"]["结构化记录_SHA256"] = h
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, fout)

    # 汇总统计
    cross_theme_items = {c for c, ts in id_themes.items() if len(ts) > 1}
    g = {
        "input": sum(s["input"] for s in per_file.values()),
        "output": sum(s["output"] for s in per_file.values()),
        "duplicates": sum(s["duplicates"] for s in per_file.values()),
        "rejects": {k: sum(s["rejects"][k] for s in per_file.values())
                    for k in ("bad_json", "no_audit_item", "missing_code_or_name")},
        "unique_items": len(seen),
        "cross_theme_dup_items": len(cross_theme_items),
        "cross_theme_dup_rows": sum(counts[c] - 1 for c in cross_theme_items),
        "v2_rescued": sum(s["v2_rescued"] for s in per_file.values()),
        "fallback_rescued": sum(s["fallback_rescued"] for s in per_file.values()),
        "html_normalized_rows": sum(s["html_normalized_rows"] for s in per_file.values()),
        "tab_normalized_rows": sum(s["tab_normalized_rows"] for s in per_file.values()),
    }
    g["rejects_total"] = sum(g["rejects"].values())
    report = {
        "run_id": RUN_ID,
        "schema_version": SCHEMA_VERSION,
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "source_dir": SRC_DIR,
        "outdir": args.outdir,
        "rejects_file": args.rejects,
        "files": per_file,
        "global": g,
        "fixes_applied": [
            "HTML 规范化:html.unescape 解码实体;<br>→换行;白名单标签(p/div/span/img 等)删除",
            "控制字符:\\t→单空格;删 \\x7f 及其余 C0(除 \\n)与 C1(0x80-0x9F)",
            "v2 套餐服务(special_item_type=TC,小写键 schema)分支物化,编码取 id",
            "ITEM_ID 缺失按 TASK_CODE→ROWGUID→NATION_TASK_CODE 兜底编码",
        ],
        "no_source_fields": ["申请.材料.页数", "来源.详情页URL", "来源.官方详情JSON(按约定置空)",
                             "v2: 办理.办理环节/网上办理限制说明/办理结果.说明/有效期说明/常见问答(源数据恒空或无对应键)"],
        "anomalies": {
            "html_polluted_lines": "bad_json 中 _raw 以 '<' 开头的行为采集期 HTML 错误页",
            "service_dept_law_empty": "SERVICE_DEPT_LAW/HANDLE_GUID/AUDIT_ITEM_CONDITION/AUDIT_MATERIAL_CONDITION/AUDIT_SPTL 恒空;法律依据取自 AUDIT_ITEM.LAW",
            "tongyi_code_semantics": "TONGYI_CODE 为部门统一社会信用代码,非事项编码;编码取 ITEM_ID",
            "v2_online_derived": "v2 记录 可网上办理 派生自 application_channel_text 是否含'网上办理'",
        },
    }
    with open(args.report_prefix + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # Markdown 报告
    lines = ["# 法人服务物化报告", "",
             f"- 运行ID: {RUN_ID}", f"- schema: {SCHEMA_VERSION}",
             f"- 开始: {started}  结束: {report['finished_at']}  耗时: {report['elapsed_seconds']}s",
             f"- 输入: {SRC_DIR}({len(files)} 个文件)", f"- 输出: {args.outdir}",
             f"- rejects: {args.rejects}", "",
             "## 全局汇总", "",
             f"- 输入行: {g['input']}",
             f"- 输出行(去重后唯一事项): {g['output']}",
             f"- 重复行(按编码全局去重): {g['duplicates']}",
             f"- 拒绝行: {g['rejects_total']}(bad_json={g['rejects']['bad_json']}, "
             f"no_audit_item={g['rejects']['no_audit_item']}, "
             f"missing_code_or_name={g['rejects']['missing_code_or_name']})",
             f"- 跨主题重复: {g['cross_theme_dup_items']} 个事项 / {g['cross_theme_dup_rows']} 行",
             "", "## 质量审计修复项统计", "",
             f"- v2 套餐服务挽救: {g['v2_rescued']} 条",
             f"- ITEM_ID 缺失兜底挽救: {g['fallback_rescued']} 条",
             f"- HTML 实体/标签规范化影响行: {g['html_normalized_rows']} 条",
             f"- \\t→空格规范化影响行: {g['tab_normalized_rows']} 条",
             "", "## 无来源字段", ""]
    lines += [f"- {x}" for x in report["no_source_fields"]]
    lines += ["", "## 异常说明", ""]
    lines += [f"- {v}" for v in report["anomalies"].values()]
    lines += ["", "## 每文件明细", "",
              "| 文件 | 输入 | 输出 | 重复 | 拒绝 | v2挽救 | 兜底 | HTML规范化 |", "|---|---|---|---|---|---|---|---|"]
    for fn in files:
        s = per_file[fn]
        lines.append(f"| {fn} | {s['input']} | {s['output']} | {s['duplicates']} | "
                     f"{sum(s['rejects'].values())} | {s['v2_rescued']} | {s['fallback_rescued']} | {s['html_normalized_rows']} |")
    with open(args.report_prefix + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"DONE elapsed={report['elapsed_seconds']}s global={json.dumps(g, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    main()
