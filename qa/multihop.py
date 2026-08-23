#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多跳检索器（KAG 式，自研轻量实现）
==================================
机制提取自 OpenSPG/KAG solver（kg/_ref/KAG，Apache-2.0），按本项目图模型重写：
  1. LLM 规划：把问题分解为带变量别名的"跳计划"（KAG: logic_form_plan 提示词 + lf planner）
  2. 变量绑定：上一跳发现的实体绑定到别名，下一跳以这些实体为图上起点
     （KAG: KgRetrieverTemplate._find_entities 先查已绑定别名、再做实体链接）
  3. 失败回退：规划/定位失败时降级单轮检索（KAG: static pipeline 的整计划重试，
     本实现改为回退，成本更低）

与 KAG 原版的差异（刻意）：
  - 不经 KAG server（8887）的 search/graph API，直接 Neo4j Cypher；
  - 跳计划用简化 JSON（locate/traverse × ≤4），不用完整逻辑形式语法（本 schema 固定且小）；
  - 每题仅 +1 次 LLM 规划调用，无逐跳 summary——KAG 靠 summary 绑定"答案文本"，
    本实现绑定"实体节点"，对图遍历已足够。

链路：
  问题 → DeepSeek 规划跳计划 → 逐跳执行
       ├ locate   bge-m3 向量检索定位实体，绑定别名（a1/m1/...）
       └ traverse 从绑定实体出发沿白名单关系走一跳（out/in 方向）
       → 汇总上下文（多跳路径 + 事项全量扩展）→ 生成
  规划失败 / 定位无命中 / 全程未覆盖任何事项 → 自动回退单轮检索
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from retriever import (
    GovRetriever, RetrievalResult, Seed,
    IDX_AFFAIR, IDX_MATERIAL, IDX_CITATION_CONTENT, IDX_BASIS,
    clean, truncate,
)

# ---------------------------------------------------------------- 图模型白名单

# 类型键 → (Neo4j 标签, 向量索引, 中文名, 定位分数下限)；无索引的类型只能作为遍历目标
TYPE_INFO = {
    "affair":   ("GovAffair.Affair",             IDX_AFFAIR,           "事项",   0.45),
    "material": ("GovAffair.Material",           IDX_MATERIAL,         "材料",   0.45),
    "citation": ("GovAffair.LegalCitation",      IDX_CITATION_CONTENT, "法条",   0.45),
    "basis":    ("GovAffair.LegalBasis",         IDX_BASIS,            "法规",   0.72),
    "step":     ("GovAffair.ProcessStep",        None,                 "环节",   0.45),
    "result":   ("GovAffair.ResultDocument",     None,                 "办理结果", 0.45),
    "cross":    ("GovAffair.CrossRegionHandling", None,                "通办",   0.45),
    "org":      ("GovAffair.ImplementingOrg",    None,                 "部门",   0.45),
}
LOCATABLE = {"affair", "material", "citation", "basis"}

# 关系 → (出向源类型, 出向目标类型, 中文含义)；均与图中实际关系类型一一对应（db.relationshipTypes 现验）
REL_INFO = {
    "requireMaterial":    ("affair",   "material", "所需材料"),
    "hasStep":            ("affair",   "step",     "办理环节"),
    "nextStep":           ("step",     "step",     "下一环节"),
    "citeLegal":          ("affair",   "citation", "引用法条"),
    "partOf":             ("citation", "basis",    "所属法规"),
    "produceResult":      ("affair",   "result",   "办理结果"),
    "supportCrossRegion": ("affair",   "cross",    "通办范围"),
    "implementedBy":      ("affair",   "org",      "实施部门"),
}

# LLM 输出的类型/关系名归一化（大小写与中文别名 → 白名单键）
_TYPE_ALIAS = {
    "affair": "affair", "事项": "affair", "政务事项": "affair",
    "material": "material", "材料": "material",
    "citation": "citation", "法条": "citation", "条款": "citation",
    "basis": "basis", "法规": "basis", "法律依据": "basis",
}
_REL_ALIAS = {
    "requirematerial": "requireMaterial", "所需材料": "requireMaterial",
    "需要材料": "requireMaterial", "申请材料": "requireMaterial",
    "citelegal": "citeLegal", "引用法条": "citeLegal",
    "partof": "partOf", "所属法规": "partOf",
    "hasstep": "hasStep", "办理环节": "hasStep",
    "nextstep": "nextStep", "下一环节": "nextStep",
    "produceresult": "produceResult", "办理结果": "produceResult",
    "supportcrossregion": "supportCrossRegion", "通办范围": "supportCrossRegion",
    "implementedby": "implementedBy", "实施部门": "implementedBy", "实施主体": "implementedBy",
}

MAX_HOPS = 4          # 含 locate
LOCATE_K = 5          # 向量检索 top-k
TRAVERSE_LIMIT = 30   # 单跳绑定节点上限

# ---------------------------------------------------------------- 规划提示词

PLAN_SYSTEM = """你是政务知识图谱的多跳检索规划器。给定用户问题，输出一个"跳计划"JSON：\
逐跳在图上定位与遍历，最终收集回答问题所需的节点。

图模型（只能使用以下类型与关系）：
- 可定位类型（type，向量检索）：affair=政务事项、material=材料、citation=法条（按条文内容）、basis=法规（按名称）
- 关系（relation 白名单；括号内为方向说明）：
  requireMaterial: 事项→材料（out=查某事项要什么材料；in=反查哪些事项需要某材料）
  citeLegal: 事项→法条（out）；partOf: 法条→法规（out，可得到法规名与文号）
  hasStep: 事项→办理环节（out）；nextStep: 环节→环节（out）
  produceResult: 事项→办理结果（out）；supportCrossRegion: 事项→通办范围（out）；implementedBy: 事项→实施部门（out）

输出格式（只输出 JSON，不要解释、不要代码块标记）：
{"hops":[
 {"step":1,"action":"locate","type":"affair","query":"申领居住证","bind":"a1","desc":"定位事项"},
 {"step":2,"action":"traverse","from":"a1","relation":"requireMaterial","direction":"out","bind":"m1","desc":"查所需材料"},
 {"step":3,"action":"traverse","from":"m1","relation":"requireMaterial","direction":"in","bind":"a2","desc":"反查共用该材料的事项"}
]}

规则：
1. 第一步必须是 locate；总跳数（含 locate）最多 4。
2. traverse 的 from 必须引用之前某步的 bind；direction 只能是 out 或 in；relation 必须用白名单拼写。
3. bind 别名全局唯一（如 a1/m1/c1/b1/a2）；后续跳可引用任意前跳的 bind。
4. 单跳即可回答的问题不要硬凑多跳；跨事项比较、链式追问才需要多跳。
5. locate 的 query 用适合向量检索的短关键词，不要照抄整句问题。"""


class PlanError(Exception):
    """跳计划无效（解析失败 / 违反白名单 / 引用未绑定变量等）。"""


# ---------------------------------------------------------------- 结果结构

@dataclass
class MultiHopResult:
    question: str
    hops: list[dict] = field(default_factory=list)   # 逐跳轨迹（供 UI 可视化）
    seeds: list[Seed] = field(default_factory=list)  # locate 命中（与单轮同构）
    context: str = ""                                # 给 LLM 的上下文
    fallback: bool = False                           # 是否回退单轮检索
    fallback_reason: str = ""
    plan_ms: int = 0
    exec_ms: int = 0


# ---------------------------------------------------------------- 引擎

class MultiHopEngine:
    def __init__(self, retriever: GovRetriever, generator):
        """generator: GovGenerator 实例，复用其 DeepSeek client 与 key 加载。"""
        self.retriever = retriever
        self.llm = generator.client
        self.model = generator.model

    # -------- 规划 --------

    def plan(self, question: str) -> list[dict]:
        try:
            resp = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PLAN_SYSTEM},
                    {"role": "user", "content": question},
                ],
                temperature=0, max_tokens=2048, stream=False, timeout=60,
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:  # 网络/限流等，交由上层回退
            raise PlanError(f"规划调用失败: {type(e).__name__}: {e}")
        return self._validate_plan(self._extract_json(text), question)

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = re.sub(r"```(json)?", "", text).strip()
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            raise PlanError("规划输出中无 JSON")
        try:
            obj = json.loads(text[s:e + 1])
        except Exception as e:
            raise PlanError(f"规划 JSON 解析失败: {e}")
        if not isinstance(obj, dict):
            raise PlanError("规划 JSON 顶层不是对象")
        return obj

    @staticmethod
    def _validate_plan(obj: dict, question: str) -> list[dict]:
        raw = obj.get("hops")
        if not isinstance(raw, list) or not raw:
            raise PlanError("hops 为空")
        out, binds = [], set()
        for i, h in enumerate(raw[:MAX_HOPS]):
            if not isinstance(h, dict):
                continue
            action = str(h.get("action", "")).strip().lower()
            if i == 0 and action != "locate":
                raise PlanError("第一步必须是 locate")
            if action == "locate":
                t = _TYPE_ALIAS.get(str(h.get("type", "")).strip().lower())
                if t not in LOCATABLE:
                    raise PlanError(f"不可定位类型: {h.get('type')}")
                bind = str(h.get("bind") or f"n{i+1}").strip()
                out.append({
                    "step": i + 1, "action": "locate", "type": t,
                    "query": str(h.get("query") or question).strip()[:120],
                    "bind": bind, "desc": str(h.get("desc", "")).strip(),
                })
            elif action == "traverse":
                rel = _REL_ALIAS.get(str(h.get("relation", "")).strip().lower())
                if rel not in REL_INFO:
                    raise PlanError(f"关系不在白名单: {h.get('relation')}")
                frm = str(h.get("from", "")).strip()
                if frm not in binds:
                    raise PlanError(f"from 引用了未绑定的别名: {frm}")
                direction = str(h.get("direction", "out")).strip().lower()
                if direction not in ("out", "in"):
                    direction = "out"
                bind = str(h.get("bind") or f"n{i+1}").strip()
                out.append({
                    "step": i + 1, "action": "traverse", "from": frm,
                    "relation": rel, "direction": direction, "bind": bind,
                    "desc": str(h.get("desc", "")).strip(),
                })
            else:
                continue  # 未知 action 跳过
            binds.add(out[-1]["bind"])
        if not out or out[0]["action"] != "locate":
            raise PlanError("计划缺少有效的 locate 起点")
        return out

    # -------- 执行 --------

    def run(self, question: str) -> MultiHopResult:
        t0 = time.time()
        try:
            plan = self.plan(question)
        except PlanError as e:
            plan_ms = int((time.time() - t0) * 1000)
            return self._fallback(question, str(e), plan_ms)
        plan_ms = int((time.time() - t0) * 1000)

        t1 = time.time()
        trace: list[dict] = []
        vars: dict[str, dict] = {}   # 别名 → {"type", "entities"}
        with self.retriever.driver.session(database=self.retriever.db) as session:
            for h in plan:
                if h["action"] == "locate":
                    tr = self._do_locate(session, h)
                    trace.append(tr)
                    if not tr.get("bound"):     # 第一步定位失败 → 回退
                        return self._fallback(question, "首跳向量定位无命中",
                                              plan_ms, trace,
                                              exec_ms=int((time.time() - t1) * 1000))
                else:
                    tr = self._do_traverse(session, h, vars)
                    trace.append(tr)
                if tr.get("bind") and tr.get("bound") is not None:
                    vars[tr["bind"]] = {"type": tr["type"], "entities": tr["bound"]}
            result = self._assemble(session, question, trace, vars,
                                    plan_ms, exec_ms=int((time.time() - t1) * 1000))
        return result

    def _do_locate(self, session, h: dict) -> dict:
        _, idx, zh, floor = TYPE_INFO[h["type"]]
        vec = self.retriever.embed(h["query"])
        seeds = [s for s in self.retriever.vector_search(
            session, idx, vec, LOCATE_K, zh) if s.score >= floor]
        # 事项存在同名多区县实例，按名去重只绑 1 个；其余类型绑前 3 个
        cap = 1 if h["type"] == "affair" else 3
        seen, bound = set(), []
        for s in seeds:
            if s.name in seen:
                continue
            seen.add(s.name)
            bound.append({"id": s.node_id, "name": s.name})
            if len(bound) >= cap:
                break
        names = "、".join(e["name"] for e in bound[:3]) or "（无命中）"
        return {
            "step": h["step"], "action": "locate", "type": h["type"],
            "type_zh": TYPE_INFO[h["type"]][2], "query": h["query"],
            "desc": h["desc"], "bind": h["bind"],
            "found": [{"id": s.node_id, "name": truncate(s.name, 60),
                       "score": round(s.score, 4)} for s in seeds[:8]],
            "found_total": len(seeds), "bound": bound,
            "summary_line": (f"第{h['step']}跳 定位{zh}「{h['query']}」→ 命中 {len(seeds)} 个"
                             f"（绑定 {h['bind']}：{truncate(names, 80)}）"),
        }

    def _do_traverse(self, session, h: dict, vars: dict) -> dict:
        src = vars.get(h["from"])
        rel, direction = h["relation"], h["direction"]
        src_t, dst_t = REL_INFO[rel][0], REL_INFO[rel][1]
        if direction == "in":
            src_t, dst_t = dst_t, src_t
        base = {
            "step": h["step"], "action": "traverse", "from": h["from"],
            "relation": rel, "direction": direction, "type": dst_t,
            "type_zh": TYPE_INFO[dst_t][2], "desc": h["desc"], "bind": h["bind"],
            "bound": [], "found": [], "found_total": 0,
        }
        if not src or not src["entities"]:
            base["note"] = f"起点 {h['from']} 为空，本跳跳过"
            base["summary_line"] = f"第{h['step']}跳 沿 {rel} 遍历：起点为空，跳过"
            return base
        if src["type"] != src_t:
            base["note"] = f"起点类型 {src['type']} 与关系 {rel} 不匹配，本跳跳过"
            base["summary_line"] = f"第{h['step']}跳 沿 {rel} 遍历：类型不匹配，跳过"
            return base

        src_label, dst_label = TYPE_INFO[src["type"]][0], TYPE_INFO[dst_t][0]
        ids = [e["id"] for e in src["entities"]]
        if direction == "out":
            q = (f"MATCH (s:`{src_label}`)-[:{rel}]->(o:`{dst_label}`) "
                 f"WHERE s.id IN $ids "
                 f"RETURN DISTINCT o.id AS id, o.name AS name, "
                 f"o.docNo AS docNo, o.article AS article, o.content AS content "
                 f"LIMIT $lim")
        else:
            q = (f"MATCH (o:`{dst_label}`)-[:{rel}]->(s:`{src_label}`) "
                 f"WHERE s.id IN $ids "
                 f"RETURN DISTINCT o.id AS id, o.name AS name, "
                 f"o.docNo AS docNo, o.article AS article, o.content AS content "
                 f"LIMIT $lim")
        found = []
        for r in session.run(q, ids=ids, lim=TRAVERSE_LIMIT):
            e = {"id": r["id"], "name": clean(r["name"])}
            for k in ("docNo", "article"):
                if r[k]:
                    e[k] = clean(r[k])
            if r["content"]:
                e["content"] = truncate(r["content"], 200)
            found.append(e)
        arrow = "→" if direction == "out" else "←"
        base.update({
            "start": [truncate(e["name"], 40) for e in src["entities"][:4]],
            "found": [{"id": e["id"], "name": truncate(e["name"], 60)} for e in found[:12]],
            "found_total": len(found), "bound": found,
        })
        sample = "、".join(truncate(e["name"], 30) for e in found[:3])
        base["summary_line"] = (
            f"第{h['step']}跳 从 {h['from']}（{len(ids)} 个{TYPE_INFO[src['type']][2]}）"
            f"沿 {rel} {arrow} 遍历 → 得到 {len(found)} 个{TYPE_INFO[dst_t][2]}，绑定 {h['bind']}"
            + (f"（如：{sample}）" if sample else "")
        )
        return base

    # -------- 上下文组装 / 回退 --------

    def _assemble(self, session, question, trace, vars, plan_ms, exec_ms) -> MultiHopResult:
        # 事项全量扩展：后绑定的（更贴近问题意图）优先，按名去重，最多 2 个
        affairs, seen = [], set()
        for alias in reversed(list(vars)):
            v = vars[alias]
            if v["type"] != "affair":
                continue
            for e in v["entities"]:
                if e["name"] in seen:
                    continue
                seen.add(e["name"])
                ctx = self.retriever.expand_affair(session, e["id"])
                if ctx and len(affairs) < 2:
                    affairs.append(ctx)
        seeds, seen_seed = [], set()
        for t in trace:
            if t["action"] == "locate":
                for f in t["found"]:
                    if f["name"] in seen_seed:
                        continue
                    seen_seed.add(f["name"])
                    seeds.append(Seed(label=t["type_zh"], name=f["name"],
                                      score=float(f["score"]), node_id=f["id"]))

        parts = ["【多跳检索路径】"]
        for t in trace:
            parts.append(t["summary_line"])

        # 全程没绑到任何事项且最后一跳无结果 → 上下文太薄，回退
        last_bound = [t for t in trace if t.get("bound")]
        if not affairs and not last_bound:
            return self._fallback(question, "多跳未命中任何节点", plan_ms, trace,
                                  exec_ms=exec_ms, seeds=seeds)

        body = RetrievalResult(question=question, seeds=seeds, affairs=affairs)
        parts.append(body.to_prompt_context())

        # 最后一跳的非事项终点节点（含法规文号/法条内容等补充信息）
        for t in reversed(trace):
            if t["action"] == "traverse" and t["found_total"] > 0:
                if t["type"] != "affair":
                    parts.append(f"\n【多跳终点节点】（第{t['step']}跳 {t['relation']}"
                                 f"{'→' if t['direction'] == 'out' else '←'}，共 {t['found_total']} 个）")
                    for e in t["bound"][:12]:
                        line = f"- [{t['type_zh']}] {truncate(e['name'], 80)}"
                        if e.get("docNo"):
                            line += f"（文号：{truncate(e['docNo'], 60)}）"
                        if e.get("article"):
                            line += f" {truncate(e['article'], 40)}"
                        parts.append(line)
                        if e.get("content"):
                            parts.append(f"  {e['content']}")
                break
        return MultiHopResult(question=question, hops=trace, seeds=seeds,
                              context="\n".join(parts),
                              plan_ms=plan_ms, exec_ms=exec_ms)

    def _fallback(self, question, reason, plan_ms, trace=None,
                  exec_ms=0, seeds=None) -> MultiHopResult:
        t = time.time()
        ret = self.retriever.retrieve(question)
        return MultiHopResult(
            question=question, hops=trace or [], seeds=seeds or ret.seeds,
            context=ret.to_prompt_context(), fallback=True, fallback_reason=reason,
            plan_ms=plan_ms,
            exec_ms=exec_ms + int((time.time() - t) * 1000),
        )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from generator import GovGenerator

    q = sys.argv[1] if len(sys.argv) > 1 else "申领居住证和注销居住证分别需要什么材料？有哪些材料是共用的？"
    r = GovRetriever()
    g = GovGenerator()
    try:
        res = MultiHopEngine(r, g).run(q)
        for h in res.hops:
            print(f"[{h['step']}] {h.get('summary_line')}")
        print(f"fallback={res.fallback}({res.fallback_reason}) "
              f"plan={res.plan_ms}ms exec={res.exec_ms}ms")
        print("-" * 70)
        print(res.context[:2000])
    finally:
        r.close()
