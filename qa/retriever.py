#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务事项知识图谱 RAG 检索器
============================
检索链路：
  用户问题 → bge-m3 向量化 → Neo4j 向量索引 top-k（事项/材料/法条/法规 四类并行）
  → 命中种子节点 → Cypher 图扩展（1-2 跳）→ 结构化上下文

图模型（GovAffair v0.2）：
  (Affair)-[:requireMaterial]->(Material)
  (Affair)-[:hasStep]->(ProcessStep)-[:nextStep]->(ProcessStep)
  (Affair)-[:citeLegal]->(LegalCitation)-[:partOf]->(LegalBasis)
  (Affair)-[:produceResult]->(ResultDocument)
  Affair 富属性：acceptCondition / windowProcess / onlineProcess /
                 legalTimeLimit / promiseTimeLimit / handleAddress /
                 consultPhone / complaintPhone / isCharge / handleMethods
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from neo4j import GraphDatabase
from openai import OpenAI

# ---------------------------------------------------------------- 配置

NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_AUTH = ("neo4j", "neo4j@openspg")
NEO4J_DB = "govaffair"

EMBED_BASE_URL = "http://127.0.0.1:11434/v1"
EMBED_MODEL = "bge-m3"
EMBED_KEY = "ollama"

# 各类型的向量索引（_name 级；法条另有 content 级）
IDX_AFFAIR = "_gov_affair_affair_name_vector_index"
IDX_MATERIAL = "_gov_affair_material_name_vector_index"
IDX_CITATION_NAME = "_gov_affair_legal_citation_name_vector_index"
IDX_CITATION_CONTENT = "_gov_affair_legal_citation_content_vector_index"
IDX_BASIS = "_gov_affair_legal_basis_name_vector_index"

# 上下文截断长度（字符）
MAX_TEXT = 600          # Affair 长文本属性
MAX_CITATION = 400      # 单条法条内容
MAX_MATERIALS = 20      # 单事项材料条数上限
MAX_STEPS = 30          # 单事项步骤条数上限
MAX_CITATIONS_PER_AFFAIR = 10

QUOTE_RE = re.compile(r'^["\']+|["\']+$')


def clean(v):
    """KAG 写入的 name 值带首尾引号，统一清理；None 安全。"""
    if v is None:
        return ""
    return QUOTE_RE.sub("", str(v)).strip()


def truncate(text: str, limit: int) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------- 数据结构

@dataclass
class Seed:
    """向量检索命中的种子节点。"""
    label: str          # 人类可读类型名
    name: str
    score: float
    node_id: str        # 节点 id 属性（KAG 写入的业务 id）


@dataclass
class RetrievalResult:
    """检索+扩展后的结构化上下文。"""
    question: str
    seeds: list[Seed] = field(default_factory=list)
    affairs: list[dict] = field(default_factory=list)       # 事项全量上下文
    extra_materials: list[dict] = field(default_factory=list)   # 反查的材料命中
    extra_citations: list[dict] = field(default_factory=list)   # 反查的法条命中

    def to_prompt_context(self) -> str:
        """渲染为给 LLM 的上下文文本。"""
        parts: list[str] = []

        parts.append("【向量检索命中（相关度得分）】")
        for s in self.seeds:
            parts.append(f"- [{s.label}] {s.name}（{s.score:.4f}）")

        for i, a in enumerate(self.affairs, 1):
            parts.append(f"\n【事项 {i}】{a['name']}")
            if a.get("acceptCondition"):
                parts.append(f"受理条件：{a['acceptCondition']}")
            if a.get("materials"):
                parts.append(f"所需材料（{len(a['materials'])} 项）：")
                for j, m in enumerate(a["materials"], 1):
                    parts.append(f"  {j}. {m}")
            if a.get("steps"):
                parts.append(f"办理流程（{len(a['steps'])} 步）：")
                for s in a["steps"]:
                    line = f"  {s['index']}. {s['name']}"
                    if s.get("timeLimit"):
                        line += f"（时限：{s['timeLimit']}）"
                    parts.append(line)
            if a.get("windowProcess"):
                parts.append(f"窗口办理流程：{a['windowProcess']}")
            if a.get("onlineProcess"):
                parts.append(f"网上办理流程：{a['onlineProcess']}")
            facts = []
            if a.get("legalTimeLimit"):
                facts.append(f"法定时限：{a['legalTimeLimit']}")
            if a.get("promiseTimeLimit"):
                facts.append(f"承诺时限：{a['promiseTimeLimit']}")
            if a.get("isCharge"):
                facts.append(f"是否收费：{a['isCharge']}")
            if a.get("handleAddress"):
                facts.append(f"办理地点：{a['handleAddress']}")
            if a.get("consultPhone"):
                facts.append(f"咨询电话：{a['consultPhone']}")
            if facts:
                parts.append("办理信息：" + "；".join(facts))
            if a.get("crossRegion"):
                desc = []
                for c in a["crossRegion"]:
                    d = c["name"]
                    if c.get("coverRegion"):
                        d += f"（覆盖：{c['coverRegion']}）"
                    if c.get("throughForm"):
                        d += f"（形式：{c['throughForm']}）"
                    desc.append(d)
                parts.append("通办范围：" + "；".join(desc))
            if a.get("citations"):
                parts.append(f"法律依据（{len(a['citations'])} 条）：")
                for c in a["citations"]:
                    head = f"  - {c['basis']}"
                    if c.get("docNo"):
                        head += f"（{c['docNo']}）"
                    if c.get("article"):
                        head += f" {c['article']}"
                    parts.append(head)
                    if c.get("content"):
                        parts.append(f"    {c['content']}")

        if self.extra_materials:
            parts.append("\n【相关材料及对应事项】")
            for m in self.extra_materials:
                line = f"- {m['name']}"
                if m.get("affairs"):
                    line += f"（需要它的事项：{'、'.join(m['affairs'][:5])}）"
                parts.append(line)

        if self.extra_citations:
            parts.append("\n【相关法规条文】")
            for c in self.extra_citations:
                head = f"- {c['basis']}"
                if c.get("docNo"):
                    head += f"（{c['docNo']}）"
                if c.get("article"):
                    head += f" {c['article']}"
                parts.append(head)
                if c.get("content"):
                    parts.append(f"  {c['content']}")
                if c.get("affairs"):
                    parts.append(f"  引用事项：{'、'.join(c['affairs'][:5])}")

        return "\n".join(parts)


# ---------------------------------------------------------------- 检索器

class GovRetriever:
    def __init__(
        self,
        neo4j_uri: str = NEO4J_URI,
        neo4j_auth: tuple = NEO4J_AUTH,
        neo4j_db: str = NEO4J_DB,
        embed_base_url: str = EMBED_BASE_URL,
        embed_model: str = EMBED_MODEL,
    ):
        self.db = neo4j_db
        self.driver = GraphDatabase.driver(neo4j_uri, auth=neo4j_auth)
        self.embedder = OpenAI(api_key=EMBED_KEY, base_url=embed_base_url, timeout=30)
        self.embed_model = embed_model

    def close(self):
        self.driver.close()

    # ---------------- 向量化 ----------------

    def embed(self, text: str) -> list[float]:
        r = self.embedder.embeddings.create(model=self.embed_model, input=text)
        return r.data[0].embedding

    # ---------------- 向量检索 ----------------

    def vector_search(self, session, index: str, vec: list[float], k: int,
                      label: str, node_where: str = "") -> list[Seed]:
        where = f"WHERE {node_where}" if node_where else ""
        q = (
            f"CALL db.index.vector.queryNodes($index, $k, $vec) "
            f"YIELD node, score WITH node, score {where} "
            f"RETURN node.name AS name, node.id AS id, score ORDER BY score DESC"
        )
        seeds = []
        for r in session.run(q, index=index, k=k, vec=vec):
            seeds.append(Seed(label=label, name=clean(r["name"]),
                              score=float(r["score"]), node_id=r["id"]))
        return seeds

    # ---------------- 图扩展 ----------------

    def expand_affair(self, session, affair_id: str) -> dict | None:
        """以事项为中心拉取全量上下文。"""
        rec = session.run(
            "MATCH (a:`GovAffair.Affair`) WHERE a.id = $id RETURN a", id=affair_id
        ).single()
        if not rec:
            return None
        a = rec["a"]
        ctx = {
            "name": clean(a.get("name")),
            "acceptCondition": truncate(a.get("acceptCondition"), MAX_TEXT),
            "windowProcess": truncate(a.get("windowProcess"), MAX_TEXT),
            "onlineProcess": truncate(a.get("onlineProcess"), MAX_TEXT),
            "legalTimeLimit": clean(a.get("legalTimeLimit")),
            "promiseTimeLimit": clean(a.get("promiseTimeLimit")),
            "isCharge": clean(a.get("isCharge")),
            "handleAddress": truncate(a.get("handleAddress"), 200),
            "consultPhone": clean(a.get("consultPhone")),
        }
        # 材料（去重）
        mats = session.run(
            "MATCH (a:`GovAffair.Affair` {id:$id})-[:requireMaterial]->(m:`GovAffair.Material`) "
            "RETURN DISTINCT m.name AS n LIMIT $lim",
            id=affair_id, lim=MAX_MATERIALS,
        )
        seen, ctx["materials"] = set(), []
        for r in mats:
            n = clean(r["n"])
            if n and n not in seen:
                seen.add(n)
                ctx["materials"].append(n)
        # 步骤链（按 stepIndex 排序）
        steps = session.run(
            "MATCH (a:`GovAffair.Affair` {id:$id})-[:hasStep]->(s:`GovAffair.ProcessStep`) "
            "RETURN s.stepIndex AS i, s.name AS n, s.timeLimit AS t "
            "ORDER BY i LIMIT $lim",
            id=affair_id, lim=MAX_STEPS,
        )
        ctx["steps"] = [
            {"index": clean(r["i"]), "name": clean(r["n"]), "timeLimit": clean(r["t"])}
            for r in steps
        ]
        # 法条 → 法规（title=法规名，docNo=文号）
        cits = session.run(
            "MATCH (a:`GovAffair.Affair` {id:$id})-[:citeLegal]->(c:`GovAffair.LegalCitation`)"
            "-[:partOf]->(b:`GovAffair.LegalBasis`) "
            "RETURN c.article AS art, c.content AS ct, b.title AS bt, b.docNo AS dn "
            "LIMIT $lim",
            id=affair_id, lim=MAX_CITATIONS_PER_AFFAIR,
        )
        ctx["citations"] = [
            {"article": clean(r["art"]), "content": truncate(r["ct"], MAX_CITATION),
             "basis": clean(r["bt"]), "docNo": clean(r["dn"])}
            for r in cits
        ]
        # 通办范围（跨省/跨市/跨区通办）
        cross = session.run(
            "MATCH (a:`GovAffair.Affair` {id:$id})-[:supportCrossRegion]->(c:`GovAffair.CrossRegionHandling`) "
            "RETURN DISTINCT c.name AS n, c.coverRegion AS cr, c.throughForm AS tf LIMIT 6",
            id=affair_id,
        )
        ctx["crossRegion"] = [
            {"name": clean(r["n"]), "coverRegion": clean(r["cr"]), "throughForm": clean(r["tf"])}
            for r in cross
        ]
        return ctx

    def expand_material(self, session, material_id: str) -> dict | None:
        """材料命中 → 反查需要它的事项。"""
        rec = session.run(
            "MATCH (m:`GovAffair.Material`) WHERE m.id = $id RETURN m.name AS n", id=material_id
        ).single()
        if not rec:
            return None
        affairs = session.run(
            "MATCH (a:`GovAffair.Affair`)-[:requireMaterial]->(m:`GovAffair.Material` {id:$id}) "
            "RETURN DISTINCT a.name AS n LIMIT 8",
            id=material_id,
        )
        return {"name": clean(rec["n"]), "affairs": [clean(r["n"]) for r in affairs]}

    def expand_citation(self, session, citation_id: str) -> dict | None:
        """法条命中 → partOf 法规 + 反查引用事项。"""
        rec = session.run(
            "MATCH (c:`GovAffair.LegalCitation`) WHERE c.id = $id "
            "OPTIONAL MATCH (c)-[:partOf]->(b:`GovAffair.LegalBasis`) "
            "RETURN c.name AS n, c.article AS art, c.content AS ct, "
            "b.title AS bt, b.docNo AS dn",
            id=citation_id,
        ).single()
        if not rec:
            return None
        affairs = session.run(
            "MATCH (a:`GovAffair.Affair`)-[:citeLegal]->(c:`GovAffair.LegalCitation` {id:$id}) "
            "RETURN DISTINCT a.name AS n LIMIT 6",
            id=citation_id,
        )
        return {
            "name": clean(rec["n"]),
            "article": clean(rec["art"]),
            "content": truncate(rec["ct"], MAX_CITATION),
            "basis": clean(rec["bt"]),
            "docNo": clean(rec["dn"]),
            "affairs": [clean(r["n"]) for r in affairs],
        }

    # ---------------- 主入口 ----------------

    def retrieve(self, question: str,
                 k_affair: int = 5, k_material: int = 5,
                 k_citation: int = 5, k_basis: int = 3) -> RetrievalResult:
        vec = self.embed(question)
        result = RetrievalResult(question=question)

        with self.driver.session(database=self.db) as session:
            # 1) 四类并行向量检索
            affair_seeds = self.vector_search(session, IDX_AFFAIR, vec, k_affair, "事项")
            material_seeds = self.vector_search(session, IDX_MATERIAL, vec, k_material, "材料")
            cit_seeds = self.vector_search(session, IDX_CITATION_CONTENT, vec, k_citation, "法条(内容)")
            cit_name_seeds = self.vector_search(session, IDX_CITATION_NAME, vec, max(2, k_citation // 2), "法条(名)")
            basis_seeds = self.vector_search(session, IDX_BASIS, vec, k_basis, "法规")

            # 过滤极低分（向量索引返回的余弦相似度，<0.5 基本无关）
            FLOOR = 0.45
            affair_seeds = [s for s in affair_seeds if s.score >= FLOOR]
            material_seeds = [s for s in material_seeds if s.score >= FLOOR]
            cit_seeds = [s for s in cit_seeds if s.score >= FLOOR]
            basis_seeds = [s for s in basis_seeds if s.score >= FLOOR]

            result.seeds = affair_seeds + material_seeds + cit_seeds + basis_seeds
            result.seeds.sort(key=lambda s: -s.score)

            # 2) 事项种子 → 全量扩展（同名事项是多区县实例，内容近似，按名称去重）
            seen_affair_names = set()
            expanded = 0
            for s in affair_seeds:
                if expanded >= 2 or s.name in seen_affair_names:
                    continue
                ctx = self.expand_affair(session, s.node_id)
                if ctx:
                    result.affairs.append(ctx)
                    seen_affair_names.add(ctx["name"])
                    expanded += 1

            # 3) 材料种子反查；若某材料的主要事项已在扩展列表且其材料已覆盖则跳过
            for s in material_seeds[:4]:
                m = self.expand_material(session, s.node_id)
                if not m:
                    continue
                # 若该材料已出现在已扩展事项的材料清单里，不重复列
                if any(m["name"] in a["materials"] for a in result.affairs):
                    continue
                result.extra_materials.append(m)

            # 4) 法条种子扩展（去重：事项扩展里已包含的法条跳过）
            covered = {c["article"] + c["basis"] for a in result.affairs for c in a["citations"]}
            seen_cit = set()
            for s in (cit_seeds + cit_name_seeds)[:6]:
                c = self.expand_citation(session, s.node_id)
                if not c:
                    continue
                key = c["article"] + c["basis"]
                if key in covered or key in seen_cit:
                    continue
                seen_cit.add(key)
                result.extra_citations.append(c)

            # 5) 法规种子：得分门槛更高（名称索引易混入无关文号），且仅补未覆盖的；显示用 title（法规名）
            basis_seeds = [s for s in basis_seeds if s.score >= 0.8]
            covered_basis = {c["basis"] for c in result.extra_citations}
            covered_basis |= {c["basis"] for a in result.affairs for c in a["citations"]}
            for s in basis_seeds:
                rec = session.run(
                    "MATCH (b:`GovAffair.LegalBasis`) WHERE b.id = $id "
                    "RETURN b.title AS bt, b.docNo AS dn", id=s.node_id,
                ).single()
                title = clean(rec["bt"]) if rec else s.name
                doc_no = clean(rec["dn"]) if rec else ""
                if title in covered_basis:
                    continue
                result.extra_citations.append(
                    {"name": title, "article": "", "content": "", "basis": title,
                     "docNo": doc_no, "affairs": []}
                )
                covered_basis.add(title)

        return result


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "申领居住证需要提交哪些材料？"
    r = GovRetriever()
    try:
        res = r.retrieve(q)
        print(res.to_prompt_context())
    finally:
        r.close()
