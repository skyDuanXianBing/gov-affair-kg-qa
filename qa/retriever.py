#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务事项知识图谱 RAG 检索器
============================
检索链路：
  用户问题 → bge-m3 向量化 → Neo4j 向量索引 top-k（事项/材料/法条/法规 四类并行）
  → 命中种子节点 → Cypher 图扩展（1-2 跳）→ 结构化上下文

通过环境变量 KG_SCHEMA（govaffair | zwdmxgj，默认 govaffair）在两套图模型间切换：
  govaffair（默认）：旧图 GovAffair v0.2，库 "govaffair"，行为与单图版本完全一致
  zwdmxgj        ：新图 ZwdmxGJ v0.3，库 "zwdmxgj"
映射依据：docs/GovAffair到ZwdmxGJ迁移映射.md。

旧图模型（GovAffair v0.2）：
  (Affair)-[:requireMaterial]->(Material)
  (Affair)-[:hasStep]->(ProcessStep)-[:nextStep]->(ProcessStep)
  (Affair)-[:citeLegal]->(LegalCitation)-[:partOf]->(LegalBasis)
  (Affair)-[:produceResult]->(ResultDocument)
  Affair 富属性：acceptCondition / windowProcess / onlineProcess /
                 legalTimeLimit / promiseTimeLimit / handleAddress /
                 consultPhone / complaintPhone / isCharge / handleMethods
新图模型（ZwdmxGJ v0.3）的差异（qa/ 检索面）：
  Affair→GovernmentService、ResultDocument→ServiceResult、ImplementingOrg→Department；
  requireMaterial→requiresMaterial、hasStep→hasProcessStep（排序改边属性 orderNo）、
  citeLegal→citesLegal、produceResult→producesResult、implementedBy→handledBy；
  CrossRegionHandling/supportCrossRegion 已删除（摘除通办段）；
  acceptCondition/windowProcess/onlineProcess 等长文本改由 hasChunk→Chunk 承载，
  isCharge→hasFee→Fee、handleAddress→hasChannel→ServiceChannel；
  LegalBasis 属性 title→name、docNo→documentNumber；法条去重键改共享 citationId。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from neo4j import GraphDatabase
from openai import OpenAI

# ---------------------------------------------------------------- 配置

NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_AUTH = ("neo4j", "neo4j@openspg")


def _resolve_kg_schema() -> str:
    """图模型开关（迁移映射 §5）：KG_SCHEMA=govaffair | zwdmxgj，默认 govaffair。

    环境变量在模块导入时读取一次；未知取值直接抛 ValueError（fail-fast），
    避免拼错时静默查错图。测试通过 importlib.reload 切换。
    """
    raw = os.environ.get("KG_SCHEMA", "govaffair")
    value = raw.strip().lower()
    if value not in ("govaffair", "zwdmxgj"):
        raise ValueError(f"KG_SCHEMA 取值无效：{raw!r}（仅支持 govaffair | zwdmxgj）")
    return value


KG_SCHEMA = _resolve_kg_schema()

# Neo4j 库名（迁移映射 §4.1：法人/个人两域同库共 namespace，靠 ServiceDomain 区分）
KG_DB_NAME = "govaffair" if KG_SCHEMA == "govaffair" else "zwdmxgj"
NEO4J_DB = KG_DB_NAME

# 事项/服务类型键（govaffair=affair，zwdmxgj=service）；multihop 主类型与其保持一致
SERVICE_TYPE = "affair" if KG_SCHEMA == "govaffair" else "service"

EMBED_BASE_URL = "http://127.0.0.1:11434/v1"
EMBED_MODEL = "bge-m3"
EMBED_KEY = "ollama"

# ---------------------------------------------------------------- 图模型变体表

# 向量索引全名（迁移映射 §4.2，共 10 条；zwdmxgj 的名字不是简单前缀替换，逐条全名映射）。
# zwdmxgj 的名称级索引（*_name_vector_index）新 schema 仅标 Text、需手工建；
# 未建时常量仍按全名给出，运行时由 Neo4j 报错兜底，不做静默降级。
INDEX_VARIANTS = {
    "govaffair": {
        "service_name": "_gov_affair_affair_name_vector_index",
        "material_name": "_gov_affair_material_name_vector_index",
        "citation_name": "_gov_affair_legal_citation_name_vector_index",
        "citation_content": "_gov_affair_legal_citation_content_vector_index",
        "basis_name": "_gov_affair_legal_basis_name_vector_index",
        # 以下能力旧图没有（govaffair 模式不参与检索）
        "chunk_content": None,
        "condition_statement": None,
        "step_check_standard": None,
        "faq_answer": None,
        "proposition_statement": None,
    },
    "zwdmxgj": {
        "service_name": "_zwdmx_g_j_government_service_name_vector_index",
        "material_name": "_zwdmx_g_j_material_name_vector_index",
        "citation_name": "_zwdmx_g_j_legal_citation_name_vector_index",
        "citation_content": "_zwdmx_g_j_legal_citation_content_vector_index",
        "basis_name": "_zwdmx_g_j_legal_basis_name_vector_index",
        "chunk_content": "_zwdmx_g_j_chunk_content_vector_index",
        "condition_statement": "_zwdmx_g_j_service_condition_statement_vector_index",
        "step_check_standard": "_zwdmx_g_j_process_step_check_standard_vector_index",
        "faq_answer": "_zwdmx_g_j_f_a_q_answer_vector_index",
        "proposition_statement": "_zwdmx_g_j_proposition_statement_vector_index",
    },
}
INDEXES = INDEX_VARIANTS[KG_SCHEMA]

# 各类型的向量索引（_name 级；法条另有 content 级）。govaffair 下 5 个旧名原样保留；
# IDX_SERVICE_NAME 为语义别名（zwdmxgj 下即 GovernmentService.name 索引）。
IDX_AFFAIR = INDEXES["service_name"]
IDX_SERVICE_NAME = IDX_AFFAIR
IDX_MATERIAL = INDEXES["material_name"]
IDX_CITATION_NAME = INDEXES["citation_name"]
IDX_CITATION_CONTENT = INDEXES["citation_content"]
IDX_BASIS = INDEXES["basis_name"]
# zwdmxgj 新增能力索引（govaffair 模式下为 None）
IDX_CHUNK = INDEXES["chunk_content"]
IDX_CONDITION = INDEXES["condition_statement"]
IDX_STEP_CHECK_STANDARD = INDEXES["step_check_standard"]
IDX_FAQ = INDEXES["faq_answer"]
IDX_PROPOSITION = INDEXES["proposition_statement"]

# Neo4j 标签：前缀 + 短名集中配置（迁移映射 §1.1、§6 未决项 3）。
# 旧图实测为 "GovAffair.Affair" 形式；新图前缀按同一惯例假设为 "ZwdmxGJ."，
# 若灌图后 CALL db.labels() 实测为短名（如 GovernmentService），仅改 zwdmxgj 的 prefix 一处即可。
LABEL_VARIANTS = {
    "govaffair": {
        "prefix": "GovAffair.",
        "short": {
            "affair": "Affair", "material": "Material", "citation": "LegalCitation",
            "basis": "LegalBasis", "step": "ProcessStep", "result": "ResultDocument",
            "cross": "CrossRegionHandling", "org": "ImplementingOrg",
        },
    },
    "zwdmxgj": {
        "prefix": "ZwdmxGJ.",
        "short": {
            "service": "GovernmentService", "material": "Material",
            "citation": "LegalCitation", "basis": "LegalBasis", "step": "ProcessStep",
            "result": "ServiceResult", "department": "Department", "chunk": "Chunk",
            "condition": "ServiceCondition", "faq": "FAQ", "proposition": "Proposition",
            "channel": "ServiceChannel", "fee": "Fee", "category": "ServiceCategory",
            "domain": "ServiceDomain",
        },
    },
}
LABEL_PREFIX = LABEL_VARIANTS[KG_SCHEMA]["prefix"]
# 类型键 → 完整标签（当前变体）；expand_* 与 multihop 遍历共用此表
LABELS = {k: LABEL_PREFIX + s for k, s in LABEL_VARIANTS[KG_SCHEMA]["short"].items()}

# 类型键 → 节点 id 属性名。旧图统一为 KAG 业务 id；新图为共享/私有 id
# （serviceId/materialId/citationId/legalBasisId/...，见 schemas/ZwdmxGJ-v0.3.schema 头注）。
ID_PROP_VARIANTS = {
    "govaffair": {k: "id" for k in LABEL_VARIANTS["govaffair"]["short"]},
    "zwdmxgj": {
        "service": "serviceId", "material": "materialId", "citation": "citationId",
        "basis": "legalBasisId", "step": "processStepId", "result": "resultId",
        "department": "departmentId", "chunk": "chunkId", "condition": "conditionId",
        "faq": "faqId", "proposition": "propositionId", "channel": "channelId",
        "fee": "feeId", "category": "categoryId", "domain": "domainId",
    },
}
ID_PROP = ID_PROP_VARIANTS[KG_SCHEMA]

# 上下文截断长度（字符）
MAX_TEXT = 600          # Affair 长文本属性
MAX_CITATION = 400      # 单条法条内容
MAX_MATERIALS = 20      # 单事项材料条数上限
MAX_STEPS = 30          # 单事项步骤条数上限
MAX_CITATIONS_PER_AFFAIR = 10
# zwdmxgj 扩展段上限
MAX_DEPARTMENTS = 5     # 单事项主管部门数上限
MAX_CHANNELS = 6        # 单事项办理渠道数上限
MAX_FEES = 3            # 单事项收费条目数上限
MAX_FAQS = 5            # 单事项常见问答条数上限
MAX_CHUNKS = 24         # 单事项拉取的原文块数（含按 sourceField 归类）
MAX_EXTRA_CHUNKS = 6    # 归不进专有字段的原文块进入上下文的条数

QUOTE_RE = re.compile(r'^["\']+|["\']+$')


def clean(v):
    """KAG 写入的 name 值带首尾引号，统一清理；None 安全。"""
    if v is None:
        return ""
    return QUOTE_RE.sub("", str(v)).strip()


def truncate(text: str, limit: int) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[:limit] + "…"


def citation_dedup_key(c: dict) -> str:
    """法条上下文去重键（迁移映射 §3.1#13）：
    zwdmxgj 用共享 citationId；govaffair 沿用 条款+法规名。"""
    if KG_SCHEMA == "zwdmxgj" and c.get("citationId"):
        return c["citationId"]
    return c["article"] + c["basis"]


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
        """渲染为给 LLM 的上下文文本。

        departments/channels/fees/faqs/chunks 为 zwdmxgj 模式新增段，
        仅在 ctx 携带对应键时渲染（govaffair 输出不受影响）。
        """
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
            if a.get("departments"):
                parts.append("主管部门：" + "、".join(a["departments"][:MAX_DEPARTMENTS]))
            if a.get("channels"):
                parts.append(f"办理渠道（{len(a['channels'])} 个）：")
                for c in a["channels"]:
                    line = f"  - {c['name']}"
                    if c.get("type"):
                        line += f"（{c['type']}）"
                    if c.get("serviceTime"):
                        line += f" 服务时间：{c['serviceTime']}"
                    parts.append(line)
            if a.get("fees"):
                fee_lines = []
                for f in a["fees"]:
                    seg = []
                    if f.get("status"):
                        seg.append(f["status"])
                    if f.get("standard"):
                        seg.append(f"标准：{f['standard']}")
                    if f.get("basis"):
                        seg.append(f"依据：{f['basis']}")
                    name = f["name"] or "收费"
                    fee_lines.append(name + (f"（{'；'.join(seg)}）" if seg else ""))
                parts.append("收费信息：" + "；".join(fee_lines))
            if a.get("faqs"):
                parts.append(f"常见问答（{len(a['faqs'])} 条）：")
                for f in a["faqs"]:
                    parts.append(f"  问：{f['question']}")
                    parts.append(f"  答：{f['answer']}")
            if a.get("chunks"):
                parts.append(f"原文摘录（{len(a['chunks'])} 段）：")
                for c in a["chunks"]:
                    tag = f"[{c['sourceField']}] " if c.get("sourceField") else ""
                    parts.append(f"  - {tag}{c['content']}")
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
                      label: str, node_where: str = "", id_prop: str = "id") -> list[Seed]:
        """id_prop：节点 id 属性名（govaffair 统一 "id"；zwdmxgj 为 serviceId/...）。"""
        where = f"WHERE {node_where}" if node_where else ""
        q = (
            f"CALL db.index.vector.queryNodes($index, $k, $vec) "
            f"YIELD node, score WITH node, score {where} "
            f"RETURN node.name AS name, node.{id_prop} AS id, score ORDER BY score DESC"
        )
        seeds = []
        for r in session.run(q, index=index, k=k, vec=vec):
            seeds.append(Seed(label=label, name=clean(r["name"]),
                              score=float(r["score"]), node_id=r["id"]))
        return seeds

    # ---------------- 图扩展 ----------------

    def expand_affair(self, session, affair_id: str) -> dict | None:
        """以事项为中心拉取全量上下文（按 KG_SCHEMA 分派到对应图实现）。"""
        if KG_SCHEMA == "zwdmxgj":
            return self._expand_affair_zwdmxgj(session, affair_id)
        return self._expand_affair_govaffair(session, affair_id)

    def _expand_affair_govaffair(self, session, affair_id: str) -> dict | None:
        """GovAffair 旧图：行为与单图版本完全一致。"""
        rec = session.run(
            f"MATCH (a:`{LABELS['affair']}`) WHERE a.id = $id RETURN a", id=affair_id
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
            f"MATCH (a:`{LABELS['affair']}` {{id:$id}})-[:requireMaterial]->(m:`{LABELS['material']}`) "
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
            f"MATCH (a:`{LABELS['affair']}` {{id:$id}})-[:hasStep]->(s:`{LABELS['step']}`) "
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
            f"MATCH (a:`{LABELS['affair']}` {{id:$id}})-[:citeLegal]->(c:`{LABELS['citation']}`)"
            f"-[:partOf]->(b:`{LABELS['basis']}`) "
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
            f"MATCH (a:`{LABELS['affair']}` {{id:$id}})-[:supportCrossRegion]->(c:`{LABELS['cross']}`) "
            "RETURN DISTINCT c.name AS n, c.coverRegion AS cr, c.throughForm AS tf LIMIT 6",
            id=affair_id,
        )
        ctx["crossRegion"] = [
            {"name": clean(r["n"]), "coverRegion": clean(r["cr"]), "throughForm": clean(r["tf"])}
            for r in cross
        ]
        return ctx

    def _expand_affair_zwdmxgj(self, session, affair_id: str) -> dict | None:
        """ZwdmxGJ 新图：GovernmentService 全量上下文。

        与旧图差异（迁移映射 §3.1）：无跨域通办段；acceptCondition/windowProcess/
        onlineProcess 长文本改由 hasChunk→Chunk（按 sourceField 归类，字段缺失时进
        "原文摘录"）；isCharge→hasFee；handleAddress→hasChannel；新增 handledBy/hasFaq。
        """
        rec = session.run(
            f"MATCH (a:`{LABELS['service']}`) WHERE a.{ID_PROP['service']} = $id RETURN a",
            id=affair_id,
        ).single()
        if not rec:
            return None
        a = rec["a"]
        ctx = {
            "name": clean(a.get("name")),
            "legalTimeLimit": clean(a.get("legalTimeLimit")),
            "promiseTimeLimit": clean(a.get("promiseTimeLimit")),
        }
        sid = f"{{{ID_PROP['service']}:$id}}"
        # 材料（共享 materialId 后同名天然合并，DISTINCT 保留无害）
        mats = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:requiresMaterial]->(m:`{LABELS['material']}`) "
            "RETURN DISTINCT m.name AS n LIMIT $lim",
            id=affair_id, lim=MAX_MATERIALS,
        )
        seen, ctx["materials"] = set(), []
        for r in mats:
            n = clean(r["n"])
            if n and n not in seen:
                seen.add(n)
                ctx["materials"].append(n)
        # 步骤链（排序键从节点 stepIndex 改为边属性 orderNo）
        steps = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[er:hasProcessStep]->(s:`{LABELS['step']}`) "
            "RETURN er.orderNo AS i, s.name AS n, s.timeLimit AS t "
            "ORDER BY i LIMIT $lim",
            id=affair_id, lim=MAX_STEPS,
        )
        ctx["steps"] = [
            {"index": clean(r["i"]), "name": clean(r["n"]), "timeLimit": clean(r["t"])}
            for r in steps
        ]
        # 法条 → 法规（name=法规名，documentNumber=文号；去重键改共享 citationId）
        cits = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:citesLegal]->(c:`{LABELS['citation']}`)"
            f"-[:partOf]->(b:`{LABELS['basis']}`) "
            "RETURN c.citationId AS cid, c.article AS art, c.content AS ct, "
            "b.name AS bt, b.documentNumber AS dn LIMIT $lim",
            id=affair_id, lim=MAX_CITATIONS_PER_AFFAIR,
        )
        ctx["citations"] = [
            {"citationId": clean(r["cid"]), "article": clean(r["art"]),
             "content": truncate(r["ct"], MAX_CITATION),
             "basis": clean(r["bt"]), "docNo": clean(r["dn"])}
            for r in cits
        ]
        # 主管部门（implementedBy → handledBy）
        deps = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:handledBy]->(d:`{LABELS['department']}`) "
            "RETURN DISTINCT d.name AS n LIMIT $lim",
            id=affair_id, lim=MAX_DEPARTMENTS,
        )
        ctx["departments"] = [clean(r["n"]) for r in deps if clean(r["n"])]
        # 办理渠道（承接旧 handleAddress 语义）
        chs = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:hasChannel]->(c:`{LABELS['channel']}`) "
            "RETURN c.name AS n, c.channelType AS t, c.serviceTime AS st, "
            "c.description AS d LIMIT $lim",
            id=affair_id, lim=MAX_CHANNELS,
        )
        ctx["channels"] = [
            {"name": truncate(r["n"], 120), "type": clean(r["t"]),
             "serviceTime": clean(r["st"]), "description": truncate(r["d"], 200)}
            for r in chs
        ]
        # 收费信息（承接旧 isCharge 标量）
        fees = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:hasFee]->(f:`{LABELS['fee']}`) "
            "RETURN f.name AS n, f.feeStatus AS fs, f.feeStandard AS fst, "
            "f.description AS d LIMIT $lim",
            id=affair_id, lim=MAX_FEES,
        )
        ctx["fees"] = [
            {"name": clean(r["n"]), "status": clean(r["fs"]),
             "standard": clean(r["fst"]), "basis": truncate(r["d"], 200)}
            for r in fees
        ]
        # 常见问答（按边 orderNo 排序）
        faqs = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[er:hasFaq]->(f:`{LABELS['faq']}`) "
            "RETURN f.name AS q, f.answer AS ans ORDER BY er.orderNo LIMIT $lim",
            id=affair_id, lim=MAX_FAQS,
        )
        ctx["faqs"] = [
            {"question": clean(r["q"]), "answer": truncate(r["ans"], MAX_TEXT)}
            for r in faqs
        ]
        # 原文块：sourceField 命中旧长文本字段的归类回填，其余进"原文摘录"
        chunks = session.run(
            f"MATCH (a:`{LABELS['service']}` {sid})-[:hasChunk]->(k:`{LABELS['chunk']}`) "
            "RETURN k.sourceField AS sf, k.chunkIndex AS ci, k.content AS ct "
            "ORDER BY ci LIMIT $lim",
            id=affair_id, lim=MAX_CHUNKS,
        )
        field_texts: dict[str, list[str]] = {}
        ctx["chunks"] = []
        for r in chunks:
            content = clean(r["ct"])
            if not content:
                continue
            sf = clean(r["sf"])
            if sf in ("acceptCondition", "windowProcess", "onlineProcess"):
                field_texts.setdefault(sf, []).append(content)
            elif len(ctx["chunks"]) < MAX_EXTRA_CHUNKS:
                ctx["chunks"].append({"sourceField": sf, "content": truncate(content, MAX_TEXT)})
        for key in ("acceptCondition", "windowProcess", "onlineProcess"):
            ctx[key] = truncate(" ".join(field_texts[key]), MAX_TEXT) if field_texts.get(key) else ""
        return ctx

    def expand_material(self, session, material_id: str) -> dict | None:
        """材料命中 → 反查需要它的事项（按 KG_SCHEMA 分派）。"""
        if KG_SCHEMA == "zwdmxgj":
            return self._expand_material_zwdmxgj(session, material_id)
        return self._expand_material_govaffair(session, material_id)

    def _expand_material_govaffair(self, session, material_id: str) -> dict | None:
        rec = session.run(
            f"MATCH (m:`{LABELS['material']}`) WHERE m.id = $id RETURN m.name AS n",
            id=material_id,
        ).single()
        if not rec:
            return None
        affairs = session.run(
            f"MATCH (a:`{LABELS['affair']}`)-[:requireMaterial]->(m:`{LABELS['material']}` {{id:$id}}) "
            "RETURN DISTINCT a.name AS n LIMIT 8",
            id=material_id,
        )
        return {"name": clean(rec["n"]), "affairs": [clean(r["n"]) for r in affairs]}

    def _expand_material_zwdmxgj(self, session, material_id: str) -> dict | None:
        rec = session.run(
            f"MATCH (m:`{LABELS['material']}`) WHERE m.{ID_PROP['material']} = $id "
            "RETURN m.name AS n",
            id=material_id,
        ).single()
        if not rec:
            return None
        affairs = session.run(
            f"MATCH (a:`{LABELS['service']}`)-[:requiresMaterial]->"
            f"(m:`{LABELS['material']}` {{{ID_PROP['material']}:$id}}) "
            "RETURN DISTINCT a.name AS n LIMIT 8",
            id=material_id,
        )
        return {"name": clean(rec["n"]), "affairs": [clean(r["n"]) for r in affairs]}

    def expand_citation(self, session, citation_id: str) -> dict | None:
        """法条命中 → partOf 法规 + 反查引用事项（按 KG_SCHEMA 分派）。"""
        if KG_SCHEMA == "zwdmxgj":
            return self._expand_citation_zwdmxgj(session, citation_id)
        return self._expand_citation_govaffair(session, citation_id)

    def _expand_citation_govaffair(self, session, citation_id: str) -> dict | None:
        rec = session.run(
            f"MATCH (c:`{LABELS['citation']}`) WHERE c.id = $id "
            f"OPTIONAL MATCH (c)-[:partOf]->(b:`{LABELS['basis']}`) "
            "RETURN c.name AS n, c.article AS art, c.content AS ct, "
            "b.title AS bt, b.docNo AS dn",
            id=citation_id,
        ).single()
        if not rec:
            return None
        affairs = session.run(
            f"MATCH (a:`{LABELS['affair']}`)-[:citeLegal]->(c:`{LABELS['citation']}` {{id:$id}}) "
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

    def _expand_citation_zwdmxgj(self, session, citation_id: str) -> dict | None:
        rec = session.run(
            f"MATCH (c:`{LABELS['citation']}`) WHERE c.{ID_PROP['citation']} = $id "
            f"OPTIONAL MATCH (c)-[:partOf]->(b:`{LABELS['basis']}`) "
            "RETURN c.name AS n, c.citationId AS cid, c.article AS art, c.content AS ct, "
            "b.name AS bt, b.documentNumber AS dn",
            id=citation_id,
        ).single()
        if not rec:
            return None
        affairs = session.run(
            f"MATCH (a:`{LABELS['service']}`)-[:citesLegal]->"
            f"(c:`{LABELS['citation']}` {{{ID_PROP['citation']}:$id}}) "
            "RETURN DISTINCT a.name AS n LIMIT 6",
            id=citation_id,
        )
        return {
            "name": clean(rec["n"]),
            "citationId": clean(rec["cid"]),
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
            # 1) 四类并行向量检索（zwdmxgj 下索引名/ id 属性按变体切换）
            affair_seeds = self.vector_search(session, IDX_AFFAIR, vec, k_affair, "事项",
                                              id_prop=ID_PROP[SERVICE_TYPE])
            material_seeds = self.vector_search(session, IDX_MATERIAL, vec, k_material, "材料",
                                                id_prop=ID_PROP["material"])
            cit_seeds = self.vector_search(session, IDX_CITATION_CONTENT, vec, k_citation, "法条(内容)",
                                           id_prop=ID_PROP["citation"])
            cit_name_seeds = self.vector_search(session, IDX_CITATION_NAME, vec, max(2, k_citation // 2),
                                                "法条(名)", id_prop=ID_PROP["citation"])
            basis_seeds = self.vector_search(session, IDX_BASIS, vec, k_basis, "法规",
                                             id_prop=ID_PROP["basis"])

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

            # 4) 法条种子扩展（去重：事项扩展里已包含的法条跳过；
            #    去重键 govaffair=条款+法规名，zwdmxgj=共享 citationId）
            covered = {citation_dedup_key(c) for a in result.affairs for c in a["citations"]}
            seen_cit = set()
            for s in (cit_seeds + cit_name_seeds)[:6]:
                c = self.expand_citation(session, s.node_id)
                if not c:
                    continue
                key = citation_dedup_key(c)
                if key in covered or key in seen_cit:
                    continue
                seen_cit.add(key)
                result.extra_citations.append(c)

            # 5) 法规种子：得分门槛更高（名称索引易混入无关文号），且仅补未覆盖的
            basis_seeds = [s for s in basis_seeds if s.score >= 0.8]
            covered_basis = {c["basis"] for c in result.extra_citations}
            covered_basis |= {c["basis"] for a in result.affairs for c in a["citations"]}
            for s in basis_seeds:
                if KG_SCHEMA == "zwdmxgj":
                    rec = session.run(
                        f"MATCH (b:`{LABELS['basis']}`) WHERE b.{ID_PROP['basis']} = $id "
                        "RETURN b.name AS bt, b.documentNumber AS dn", id=s.node_id,
                    ).single()
                else:
                    rec = session.run(
                        f"MATCH (b:`{LABELS['basis']}`) WHERE b.{ID_PROP['basis']} = $id "
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
