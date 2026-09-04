#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KG_SCHEMA 双图开关测试（不连真实 Neo4j / 不调真实 LLM）。

覆盖（qa/multihop.py、qa/retriever.py 按 docs/GovAffair到ZwdmxGJ迁移映射.md 改造）：
  1. 默认（未设 KG_SCHEMA）与显式 KG_SCHEMA=govaffair：
     库名/索引名/TYPE_INFO/REL_INFO/别名表/LOCATABLE/PLAN_SYSTEM 与旧图版本快照一致，
     expand_*、vector_search、遍历生成的 Cypher 与旧图版本逐字节一致；
  2. KG_SCHEMA=zwdmxgj：库名、10 个索引全名、TYPE_INFO（15 类）/REL_INFO（16 关系）、
     别名表、PLAN_SYSTEM、expand_* 与遍历 Cypher 使用新标签/新关系，
     且不含 GovAffair. / supportCrossRegion / cross；法条去重键改 citationId；
  3. 未知 KG_SCHEMA 取值：模块导入（reload）时直接抛 ValueError（fail-fast，已固化）。

环境变量在模块导入时读取，测试通过 importlib.reload 隔离（先 reload retriever，
再 reload multihop，保证 multihop 顶层 from-import 拿到新值）。
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

QA_DIR = Path(__file__).resolve().parents[1] / "qa"
sys.path.insert(0, str(QA_DIR))


def _ensure_importable(name: str, **attrs) -> None:
    """无 neo4j/openai 依赖的环境下装桩模块（仅测试进程内，不触真实服务）。"""
    try:
        __import__(name)
    except ImportError:
        stub = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(stub, k, v)
        sys.modules[name] = stub


_ensure_importable("neo4j", GraphDatabase=types.SimpleNamespace(driver=lambda *a, **k: None))
_ensure_importable("openai", OpenAI=object)

import retriever  # noqa: E402
import multihop   # noqa: E402


def _reload(schema_env: str | None):
    """按指定 KG_SCHEMA 环境（None=删除变量）重载 qa 模块，返回 (retriever, multihop)。"""
    if schema_env is None:
        os.environ.pop("KG_SCHEMA", None)
    else:
        os.environ["KG_SCHEMA"] = schema_env
    r = importlib.reload(retriever)
    m = importlib.reload(multihop)
    return r, m


# ---------------------------------------------------------------- 假 Neo4j 会话

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """记录 run() 的 Cypher 与参数，handler(query, params) 返回行列表。"""

    def __init__(self, handler=None):
        self.handler = handler or (lambda query, params: [])
        self.queries: list[str] = []
        self.params_list: list[dict] = []

    def run(self, query, **params):
        self.queries.append(query)
        self.params_list.append(params)
        return FakeResult(self.handler(query, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self, database=None):
        return self._session


# ---------------------------------------------------------------- 旧图（govaffair）快照
# 与改造前 qa/ 单图版本的值逐条一致（默认行为不变的回归锚点）

GOV_TYPE_INFO = {
    "affair":   ("GovAffair.Affair",             "_gov_affair_affair_name_vector_index",           "事项",   0.45),
    "material": ("GovAffair.Material",           "_gov_affair_material_name_vector_index",         "材料",   0.45),
    "citation": ("GovAffair.LegalCitation",      "_gov_affair_legal_citation_content_vector_index", "法条",   0.45),
    "basis":    ("GovAffair.LegalBasis",         "_gov_affair_legal_basis_name_vector_index",      "法规",   0.72),
    "step":     ("GovAffair.ProcessStep",        None, "环节",   0.45),
    "result":   ("GovAffair.ResultDocument",     None, "办理结果", 0.45),
    "cross":    ("GovAffair.CrossRegionHandling", None, "通办",   0.45),
    "org":      ("GovAffair.ImplementingOrg",    None, "部门",   0.45),
}
GOV_LOCATABLE = {"affair", "material", "citation", "basis"}
GOV_REL_INFO = {
    "requireMaterial":    ("affair",   "material", "所需材料"),
    "hasStep":            ("affair",   "step",     "办理环节"),
    "nextStep":           ("step",     "step",     "下一环节"),
    "citeLegal":          ("affair",   "citation", "引用法条"),
    "partOf":             ("citation", "basis",    "所属法规"),
    "produceResult":      ("affair",   "result",   "办理结果"),
    "supportCrossRegion": ("affair",   "cross",    "通办范围"),
    "implementedBy":      ("affair",   "org",      "实施部门"),
}
GOV_TYPE_ALIAS = {
    "affair": "affair", "事项": "affair", "政务事项": "affair",
    "material": "material", "材料": "material",
    "citation": "citation", "法条": "citation", "条款": "citation",
    "basis": "basis", "法规": "basis", "法律依据": "basis",
}
GOV_REL_ALIAS = {
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
GOV_PLAN_SYSTEM = (
    "你是政务知识图谱的多跳检索规划器。给定用户问题，输出一个\"跳计划\"JSON："
    "逐跳在图上定位与遍历，最终收集回答问题所需的节点。\n"
    "\n"
    "图模型（只能使用以下类型与关系）：\n"
    "- 可定位类型（type，向量检索）：affair=政务事项、material=材料、citation=法条（按条文内容）、basis=法规（按名称）\n"
    "- 关系（relation 白名单；括号内为方向说明）：\n"
    "  requireMaterial: 事项→材料（out=查某事项要什么材料；in=反查哪些事项需要某材料）\n"
    "  citeLegal: 事项→法条（out）；partOf: 法条→法规（out，可得到法规名与文号）\n"
    "  hasStep: 事项→办理环节（out）；nextStep: 环节→环节（out）\n"
    "  produceResult: 事项→办理结果（out）；supportCrossRegion: 事项→通办范围（out）；implementedBy: 事项→实施部门（out）\n"
    "\n"
    "输出格式（只输出 JSON，不要解释、不要代码块标记）：\n"
    '{"hops":[\n'
    ' {"step":1,"action":"locate","type":"affair","query":"申领居住证","bind":"a1","desc":"定位事项"},\n'
    ' {"step":2,"action":"traverse","from":"a1","relation":"requireMaterial","direction":"out","bind":"m1","desc":"查所需材料"},\n'
    ' {"step":3,"action":"traverse","from":"m1","relation":"requireMaterial","direction":"in","bind":"a2","desc":"反查共用该材料的事项"}\n'
    ']}\n'
    "\n"
    "规则：\n"
    "1. 第一步必须是 locate；总跳数（含 locate）最多 4。\n"
    "2. traverse 的 from 必须引用之前某步的 bind；direction 只能是 out 或 in；relation 必须用白名单拼写。\n"
    "3. bind 别名全局唯一（如 a1/m1/c1/b1/a2）；后续跳可引用任意前跳的 bind。\n"
    "4. 单跳即可回答的问题不要硬凑多跳；跨事项比较、链式追问才需要多跳。\n"
    "5. locate 的 query 用适合向量检索的短关键词，不要照抄整句问题。"
)
GOV_VECTOR_QUERY = ("CALL db.index.vector.queryNodes($index, $k, $vec) "
                    "YIELD node, score WITH node, score  "
                    "RETURN node.name AS name, node.id AS id, score ORDER BY score DESC")
GOV_EXPAND_AFFAIR_QUERIES = [
    "MATCH (a:`GovAffair.Affair`) WHERE a.id = $id RETURN a",
    "MATCH (a:`GovAffair.Affair` {id:$id})-[:requireMaterial]->(m:`GovAffair.Material`) "
    "RETURN DISTINCT m.name AS n LIMIT $lim",
    "MATCH (a:`GovAffair.Affair` {id:$id})-[:hasStep]->(s:`GovAffair.ProcessStep`) "
    "RETURN s.stepIndex AS i, s.name AS n, s.timeLimit AS t ORDER BY i LIMIT $lim",
    "MATCH (a:`GovAffair.Affair` {id:$id})-[:citeLegal]->(c:`GovAffair.LegalCitation`)"
    "-[:partOf]->(b:`GovAffair.LegalBasis`) "
    "RETURN c.article AS art, c.content AS ct, b.title AS bt, b.docNo AS dn LIMIT $lim",
    "MATCH (a:`GovAffair.Affair` {id:$id})-[:supportCrossRegion]->(c:`GovAffair.CrossRegionHandling`) "
    "RETURN DISTINCT c.name AS n, c.coverRegion AS cr, c.throughForm AS tf LIMIT 6",
]
GOV_EXPAND_MATERIAL_QUERIES = [
    "MATCH (m:`GovAffair.Material`) WHERE m.id = $id RETURN m.name AS n",
    "MATCH (a:`GovAffair.Affair`)-[:requireMaterial]->(m:`GovAffair.Material` {id:$id}) "
    "RETURN DISTINCT a.name AS n LIMIT 8",
]
GOV_EXPAND_CITATION_QUERIES = [
    "MATCH (c:`GovAffair.LegalCitation`) WHERE c.id = $id "
    "OPTIONAL MATCH (c)-[:partOf]->(b:`GovAffair.LegalBasis`) "
    "RETURN c.name AS n, c.article AS art, c.content AS ct, b.title AS bt, b.docNo AS dn",
    "MATCH (a:`GovAffair.Affair`)-[:citeLegal]->(c:`GovAffair.LegalCitation` {id:$id}) "
    "RETURN DISTINCT a.name AS n LIMIT 6",
]
GOV_TRAVERSE_QUERY = (
    "MATCH (s:`GovAffair.Affair`)-[:requireMaterial]->(o:`GovAffair.Material`) "
    "WHERE s.id IN $ids "
    "RETURN DISTINCT o.id AS id, o.name AS name, "
    "o.docNo AS docNo, o.article AS article, o.content AS content "
    "LIMIT $lim"
)
GOV_BASIS_SEED_QUERY = ("MATCH (b:`GovAffair.LegalBasis`) WHERE b.id = $id "
                        "RETURN b.title AS bt, b.docNo AS dn")


def _gov_rows(query: str, params: dict) -> list[dict]:
    """旧图 expand_*/法规种子查询的通用假行。"""
    if query.endswith("RETURN a"):
        return [{"a": {"name": '"居住证办理"', "legalTimeLimit": "20工作日"}}]
    if "OPTIONAL MATCH" in query:
        return [{"n": "引用", "art": "第一条", "ct": "内容", "bt": "条例", "dn": "文1"}]
    if "stepIndex AS i" in query:
        return [{"i": 1, "n": "受理", "t": "5工作日"}]
    if "supportCrossRegion" in query:
        return [{"n": "全省通办", "cr": "", "tf": ""}]
    if "requireMaterial" in query:
        return [{"n": "身份证"}]
    if query.startswith("MATCH (m:"):
        return [{"n": "身份证"}]
    if "article AS art" in query:
        return [{"art": "第一条", "ct": "内容", "bt": "条例", "dn": "文1"}]
    if "citeLegal" in query:
        return [{"n": "事项A"}]
    if "LegalBasis" in query:
        return [{"bt": "条例", "dn": "文1"}]
    return []


# ---------------------------------------------------------------- 测试基类

class SchemaSwitchTestBase(unittest.TestCase):
    """子类设 SCHEMA：None=不设环境变量 | "govaffair" | "zwdmxgj"。"""

    SCHEMA: str | None = None

    def setUp(self):
        self._saved = os.environ.get("KG_SCHEMA")
        self.r, self.m = _reload(self.SCHEMA)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("KG_SCHEMA", None)
        else:
            os.environ["KG_SCHEMA"] = self._saved
        _reload(self._saved)

    def _retriever_stub(self):
        """不连 Neo4j/嵌入服务的 GovRetriever 实例（跳过 __init__）。"""
        inst = object.__new__(self.r.GovRetriever)
        inst.db = "fake"
        inst.driver = FakeDriver(FakeSession())
        inst.embed = lambda text: [0.0]
        return inst

    def _engine(self, retriever=None):
        eng = object.__new__(self.m.MultiHopEngine)
        eng.retriever = retriever or self._retriever_stub()
        eng.llm = None
        eng.model = None
        return eng


# ---------------------------------------------------------------- 默认（未设 KG_SCHEMA）

class DefaultSchemaTests(SchemaSwitchTestBase):
    SCHEMA = None

    def test_default_schema_is_govaffair(self):
        self.assertEqual(self.r.KG_SCHEMA, "govaffair")
        self.assertEqual(self.r.KG_DB_NAME, "govaffair")
        self.assertEqual(self.r.NEO4J_DB, "govaffair")
        self.assertEqual(self.m.KG_SCHEMA, "govaffair")
        self.assertEqual(self.r.NEO4J_URI, "bolt://127.0.0.1:7687")
        self.assertEqual(self.r.EMBED_MODEL, "bge-m3")

    def test_default_indexes_are_old_five(self):
        self.assertEqual(self.r.IDX_AFFAIR, "_gov_affair_affair_name_vector_index")
        self.assertEqual(self.r.IDX_MATERIAL, "_gov_affair_material_name_vector_index")
        self.assertEqual(self.r.IDX_CITATION_NAME, "_gov_affair_legal_citation_name_vector_index")
        self.assertEqual(self.r.IDX_CITATION_CONTENT, "_gov_affair_legal_citation_content_vector_index")
        self.assertEqual(self.r.IDX_BASIS, "_gov_affair_legal_basis_name_vector_index")
        self.assertIsNone(self.r.IDX_CHUNK)
        self.assertIsNone(self.r.IDX_CONDITION)
        self.assertIsNone(self.r.IDX_FAQ)
        self.assertIsNone(self.r.IDX_PROPOSITION)

    def test_default_multihop_tables_snapshot(self):
        self.assertEqual(self.m.TYPE_INFO, GOV_TYPE_INFO)
        self.assertEqual(self.m.LOCATABLE, GOV_LOCATABLE)
        self.assertEqual(self.m.REL_INFO, GOV_REL_INFO)
        self.assertEqual(self.m._TYPE_ALIAS, GOV_TYPE_ALIAS)
        self.assertEqual(self.m._REL_ALIAS, GOV_REL_ALIAS)
        self.assertEqual(self.m.PRIMARY_TYPE, "affair")

    def test_default_plan_system_snapshot(self):
        self.assertEqual(self.m.PLAN_SYSTEM, GOV_PLAN_SYSTEM)

    def test_default_expand_affair_cypher_snapshot(self):
        inst = self._retriever_stub()
        session = FakeSession(_gov_rows)
        ctx = inst.expand_affair(session, "A-1")
        self.assertEqual(session.queries, GOV_EXPAND_AFFAIR_QUERIES)
        self.assertEqual(ctx["name"], "居住证办理")
        self.assertIn("crossRegion", ctx)  # 旧图保留通办段
        self.assertEqual(ctx["crossRegion"][0]["name"], "全省通办")

    def test_default_expand_material_and_citation_cypher_snapshot(self):
        inst = self._retriever_stub()
        session = FakeSession(_gov_rows)
        m = inst.expand_material(session, "M-1")
        self.assertEqual(session.queries, GOV_EXPAND_MATERIAL_QUERIES)
        self.assertEqual(m["name"], "身份证")
        session2 = FakeSession(_gov_rows)
        c = inst.expand_citation(session2, "C-1")
        self.assertEqual(session2.queries, GOV_EXPAND_CITATION_QUERIES)
        self.assertEqual(c["basis"], "条例")
        self.assertEqual(c["docNo"], "文1")
        self.assertNotIn("citationId", c)  # 旧图无共享 id

    def test_default_vector_search_cypher_snapshot(self):
        inst = self._retriever_stub()
        session = FakeSession(lambda q, p: [{"name": "事项A", "id": "A-1", "score": 0.9}])
        seeds = inst.vector_search(session, self.r.IDX_AFFAIR, [0.0], 5, "事项")
        self.assertEqual(session.queries[0], GOV_VECTOR_QUERY)
        self.assertEqual(seeds[0].node_id, "A-1")

    def test_default_traverse_cypher_snapshot(self):
        eng = self._engine()
        session = FakeSession(
            lambda q, p: [{"id": "M-1", "name": "身份证", "docNo": None,
                           "article": None, "content": None}])
        h = {"step": 2, "action": "traverse", "from": "a1", "relation": "requireMaterial",
             "direction": "out", "bind": "m1", "desc": ""}
        vars_ = {"a1": {"type": "affair", "entities": [{"id": "A-1", "name": "事项A"}]}}
        eng._do_traverse(session, h, vars_)
        self.assertEqual(session.queries[0], GOV_TRAVERSE_QUERY)

    def test_default_citation_dedup_key_is_article_plus_basis(self):
        self.assertEqual(
            self.r.citation_dedup_key({"article": "第一条", "basis": "条例"}),
            "第一条条例")

    def test_default_retrieve_citation_dedup(self):
        # 两个法条种子 article+basis 相同 → 只保留 1 条（旧图去重语义）
        inst = self._retriever_stub()

        def rows(query, params):
            idx = params.get("index")
            if idx == self.r.IDX_CITATION_CONTENT:
                return [{"name": "条例第一条", "id": "cit-1", "score": 0.9},
                        {"name": "条例第一条", "id": "cit-2", "score": 0.85}]
            if idx == self.r.IDX_CITATION_NAME:
                return []
            if idx == self.r.IDX_BASIS:
                return [{"name": "条例", "id": "b-1", "score": 0.9}]
            if "OPTIONAL MATCH" in query:
                return [{"n": "条例第一条", "art": "第一条", "ct": "内容",
                         "bt": "条例", "dn": "文1"}]
            return []

        session = FakeSession(rows)
        inst.driver = FakeDriver(session)
        result = inst.retrieve("办理依据")
        self.assertEqual(len(result.extra_citations), 1)  # article+basis 去重
        self.assertIn("条例第一条", result.to_prompt_context())

    def test_default_basis_seed_query_snapshot(self):
        inst = self._retriever_stub()

        def rows(query, params):
            idx = params.get("index")
            if idx == self.r.IDX_BASIS:
                return [{"name": "新法规", "id": "b-9", "score": 0.9}]
            if "LegalBasis" in query:
                return [{"bt": "新法规", "dn": "文9"}]
            return []

        session = FakeSession(rows)
        inst.driver = FakeDriver(session)
        result = inst.retrieve("某法规")
        self.assertEqual(session.queries[-1], GOV_BASIS_SEED_QUERY)
        self.assertEqual(result.extra_citations[0]["basis"], "新法规")
        self.assertEqual(result.extra_citations[0]["docNo"], "文9")

    def test_default_validate_plan_still_accepts_old_names(self):
        plan = {"hops": [
            {"step": 1, "action": "locate", "type": "事项", "query": "居住证",
             "bind": "a1", "desc": ""},
            {"step": 2, "action": "traverse", "from": "a1", "relation": "通办范围",
             "direction": "out", "bind": "x1", "desc": ""},
        ]}
        out = self.m.MultiHopEngine._validate_plan(plan, "问题")
        self.assertEqual(out[0]["type"], "affair")
        self.assertEqual(out[1]["relation"], "supportCrossRegion")


# ---------------------------------------------------------------- 显式 KG_SCHEMA=govaffair

class ExplicitGovaffairTests(SchemaSwitchTestBase):
    SCHEMA = "govaffair"

    def test_explicit_govaffair_constants_match_default(self):
        self.assertEqual(self.r.KG_SCHEMA, "govaffair")
        self.assertEqual(self.r.NEO4J_DB, "govaffair")
        self.assertEqual(self.r.IDX_AFFAIR, "_gov_affair_affair_name_vector_index")
        self.assertEqual(self.r.IDX_BASIS, "_gov_affair_legal_basis_name_vector_index")
        self.assertEqual(self.m.TYPE_INFO, GOV_TYPE_INFO)
        self.assertEqual(self.m.REL_INFO, GOV_REL_INFO)
        self.assertEqual(self.m.LOCATABLE, GOV_LOCATABLE)
        self.assertEqual(self.m.PLAN_SYSTEM, GOV_PLAN_SYSTEM)
        self.assertEqual(self.m.PRIMARY_TYPE, "affair")

    def test_explicit_govaffair_expand_affair_cypher_snapshot(self):
        inst = self._retriever_stub()
        session = FakeSession(_gov_rows)
        inst.expand_affair(session, "A-1")
        self.assertEqual(session.queries, GOV_EXPAND_AFFAIR_QUERIES)


# ---------------------------------------------------------------- KG_SCHEMA=zwdmxgj

class ZwdmxgjSchemaTests(SchemaSwitchTestBase):
    SCHEMA = "zwdmxgj"

    # ---- 基础常量 ----

    def test_schema_and_db(self):
        self.assertEqual(self.r.KG_SCHEMA, "zwdmxgj")
        self.assertEqual(self.r.KG_DB_NAME, "zwdmxgj")
        self.assertEqual(self.r.NEO4J_DB, "zwdmxgj")
        self.assertEqual(self.m.KG_SCHEMA, "zwdmxgj")
        self.assertEqual(self.m.PRIMARY_TYPE, "service")
        self.assertEqual(self.r.SERVICE_TYPE, "service")

    def test_index_full_names_follow_mapping(self):
        # 迁移映射 §4.2 的 10 个全名（非简单前缀替换）
        self.assertEqual(self.r.INDEXES, {
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
        })
        self.assertEqual(self.r.IDX_AFFAIR, "_zwdmx_g_j_government_service_name_vector_index")
        self.assertEqual(self.r.IDX_SERVICE_NAME, self.r.IDX_AFFAIR)
        self.assertEqual(self.r.IDX_CHUNK, "_zwdmx_g_j_chunk_content_vector_index")
        self.assertEqual(self.r.IDX_CONDITION, "_zwdmx_g_j_service_condition_statement_vector_index")
        self.assertEqual(self.r.IDX_FAQ, "_zwdmx_g_j_f_a_q_answer_vector_index")
        self.assertEqual(self.r.IDX_PROPOSITION, "_zwdmx_g_j_proposition_statement_vector_index")

    def test_type_info_follows_mapping(self):
        # 迁移映射 §2.3：15 类、ZwdmxGJ. 前缀、basis 门槛 0.72 沿用
        expected = {
            "service":     ("ZwdmxGJ.GovernmentService", "_zwdmx_g_j_government_service_name_vector_index", "事项", 0.45),
            "material":    ("ZwdmxGJ.Material", "_zwdmx_g_j_material_name_vector_index", "材料", 0.45),
            "citation":    ("ZwdmxGJ.LegalCitation", "_zwdmx_g_j_legal_citation_content_vector_index", "法条", 0.45),
            "basis":       ("ZwdmxGJ.LegalBasis", "_zwdmx_g_j_legal_basis_name_vector_index", "法规", 0.72),
            "chunk":       ("ZwdmxGJ.Chunk", "_zwdmx_g_j_chunk_content_vector_index", "原文块", 0.45),
            "condition":   ("ZwdmxGJ.ServiceCondition", "_zwdmx_g_j_service_condition_statement_vector_index", "办理条件", 0.45),
            "faq":         ("ZwdmxGJ.FAQ", "_zwdmx_g_j_f_a_q_answer_vector_index", "常见问答", 0.45),
            "proposition": ("ZwdmxGJ.Proposition", "_zwdmx_g_j_proposition_statement_vector_index", "命题", 0.45),
            "step":        ("ZwdmxGJ.ProcessStep", None, "环节", 0.45),
            "result":      ("ZwdmxGJ.ServiceResult", None, "办理结果", 0.45),
            "department":  ("ZwdmxGJ.Department", None, "部门", 0.45),
            "channel":     ("ZwdmxGJ.ServiceChannel", None, "渠道", 0.45),
            "fee":         ("ZwdmxGJ.Fee", None, "收费", 0.45),
            "category":    ("ZwdmxGJ.ServiceCategory", None, "分类", 0.45),
            "domain":      ("ZwdmxGJ.ServiceDomain", None, "领域", 0.45),
        }
        self.assertEqual(self.m.TYPE_INFO, expected)
        self.assertEqual(self.m.LOCATABLE,
                         {"service", "material", "citation", "basis",
                          "chunk", "condition", "faq", "proposition"})
        self.assertNotIn("cross", self.m.TYPE_INFO)

    def test_rel_info_follows_mapping(self):
        # 迁移映射 §2.3：16 关系；无 supportCrossRegion
        self.assertEqual(len(self.m.REL_INFO), 16)
        for rel, pair in {
            "requiresMaterial": ("service", "material"),
            "hasProcessStep": ("service", "step"),
            "nextStep": ("step", "step"),
            "citesLegal": ("service", "citation"),
            "partOf": ("citation", "basis"),
            "producesResult": ("service", "result"),
            "handledBy": ("service", "department"),
            "collaboratesWith": ("service", "department"),
            "hasCondition": ("service", "condition"),
            "hasChunk": ("service", "chunk"),
            "hasFaq": ("service", "faq"),
            "hasChannel": ("service", "channel"),
            "hasFee": ("service", "fee"),
            "classifiedAs": ("service", "category"),
            "belongsToDomain": ("service", "domain"),
            "statesProposition": ("service", "proposition"),
        }.items():
            self.assertEqual(self.m.REL_INFO[rel][:2], pair, rel)
        self.assertNotIn("supportCrossRegion", self.m.REL_INFO)

    def test_alias_tables_follow_mapping(self):
        self.assertEqual(self.m._TYPE_ALIAS["事项"], "service")
        self.assertEqual(self.m._TYPE_ALIAS["affair"], "service")  # 旧名兼容
        self.assertEqual(self.m._TYPE_ALIAS["原文块"], "chunk")
        self.assertEqual(self.m._TYPE_ALIAS["办理条件"], "condition")
        self.assertEqual(self.m._TYPE_ALIAS["常见问答"], "faq")
        self.assertEqual(self.m._TYPE_ALIAS["命题"], "proposition")
        self.assertEqual(self.m._REL_ALIAS["所需材料"], "requiresMaterial")
        self.assertEqual(self.m._REL_ALIAS["实施部门"], "handledBy")
        self.assertEqual(self.m._REL_ALIAS["requirematerial"], "requiresMaterial")  # 旧拼写兼容
        self.assertEqual(self.m._REL_ALIAS["implementedby"], "handledBy")
        self.assertNotIn("supportcrossregion", self.m._REL_ALIAS)
        self.assertNotIn("通办范围", self.m._REL_ALIAS)

    def test_plan_system_uses_new_model_and_drops_cross(self):
        plan = self.m.PLAN_SYSTEM
        self.assertIn("service=政务事项", plan)
        self.assertIn("requiresMaterial", plan)
        self.assertIn("citesLegal", plan)
        self.assertIn("handledBy", plan)
        self.assertIn("hasChunk", plan)
        self.assertIn('"type":"service"', plan)
        self.assertNotIn("affair=", plan)
        self.assertNotIn("GovAffair", plan)
        self.assertNotIn("supportCrossRegion", plan)
        self.assertNotIn("cross", plan.lower())
        # 共享模板段不变
        self.assertIn("1. 第一步必须是 locate；总跳数（含 locate）最多 4。", plan)

    # ---- expand_* 与遍历 Cypher ----

    def test_expand_affair_cypher_and_context(self):
        inst = self._retriever_stub()

        def rows(query, params):
            if query.endswith("RETURN a"):
                return [{"a": {"name": "居住证办理", "legalTimeLimit": "20工作日",
                               "promiseTimeLimit": "10工作日"}}]
            if "requiresMaterial" in query:
                return [{"n": "身份证"}]
            if "er:hasProcessStep" in query:
                return [{"i": 2, "n": "受理", "t": "5工作日"}]
            if "citesLegal" in query:
                return [{"cid": "LC-1", "art": "第一条", "ct": "内容",
                         "bt": "条例", "dn": "文1"}]
            if "handledBy" in query:
                return [{"n": "公安局"}]
            if "hasChannel" in query:
                return [{"n": "政务大厅", "t": "窗口", "st": "工作日", "d": ""}]
            if "hasFee" in query:
                return [{"n": "工本费", "fs": "收费", "fst": "20元", "d": "物价局"}]
            if "hasFaq" in query:
                return [{"q": "多久办完？", "ans": "10 个工作日"}]
            if "hasChunk" in query:
                return [
                    {"sf": "acceptCondition", "ci": 1, "ct": "合法稳定居住"},
                    {"sf": "windowProcess", "ci": 2, "ct": "窗口流程文本"},
                    {"sf": "", "ci": 3, "ct": "其他原文"},
                ]
            return []

        session = FakeSession(rows)
        ctx = inst.expand_affair(session, "SVC-1")
        joined = "\n".join(session.queries)
        # 新标签 / 新关系 / 新属性
        self.assertIn(":`ZwdmxGJ.GovernmentService`", joined)
        self.assertIn("a.serviceId = $id", joined)
        for rel in ("requiresMaterial", "hasProcessStep", "citesLegal", "partOf",
                    "handledBy", "hasChannel", "hasFee", "hasFaq", "hasChunk"):
            self.assertIn(rel, joined, rel)
        self.assertIn("er.orderNo AS i", joined)          # 排序改边属性 orderNo
        self.assertIn("c.citationId AS cid", joined)      # 去重键所需
        self.assertIn("b.name AS bt", joined)             # title→name
        self.assertIn("b.documentNumber AS dn", joined)   # docNo→documentNumber
        self.assertIn("k.sourceField AS sf", joined)
        # 摘除项
        self.assertNotIn("GovAffair.", joined)
        self.assertNotIn("cross", joined.lower())
        # ctx 结构
        self.assertEqual(ctx["name"], "居住证办理")
        self.assertNotIn("crossRegion", ctx)
        self.assertEqual(ctx["departments"], ["公安局"])
        self.assertEqual(ctx["materials"], ["身份证"])
        self.assertEqual(ctx["steps"][0]["index"], "2")
        self.assertEqual(ctx["citations"][0]["citationId"], "LC-1")
        self.assertEqual(ctx["citations"][0]["docNo"], "文1")
        self.assertEqual(ctx["acceptCondition"], "合法稳定居住")  # Chunk 按来源字段回填
        self.assertEqual(ctx["windowProcess"], "窗口流程文本")
        self.assertEqual(ctx["chunks"][0]["content"], "其他原文")
        self.assertEqual(ctx["faqs"][0]["question"], "多久办完？")
        self.assertEqual(ctx["fees"][0]["standard"], "20元")

    def test_expand_material_and_citation_cypher(self):
        inst = self._retriever_stub()

        def rows(query, params):
            if "requiresMaterial" in query:
                return [{"n": "事项A"}]
            if "OPTIONAL MATCH" in query:
                return [{"n": "引用", "cid": "LC-9", "art": "第一条", "ct": "内容",
                         "bt": "条例", "dn": "文1"}]
            if "citesLegal" in query:
                return [{"n": "事项A"}]
            if query.startswith("MATCH (m:"):
                return [{"n": "身份证"}]
            return []

        session = FakeSession(rows)
        m = inst.expand_material(session, "M-9")
        joined_m = "\n".join(session.queries)
        self.assertIn(":`ZwdmxGJ.Material`", joined_m)
        self.assertIn("m.materialId = $id", joined_m)
        self.assertIn("requiresMaterial", joined_m)
        self.assertEqual(m["affairs"], ["事项A"])

        session2 = FakeSession(rows)
        c = inst.expand_citation(session2, "LC-9")
        joined_c = "\n".join(session2.queries)
        self.assertIn("c.citationId = $id", joined_c)
        self.assertIn("citesLegal", joined_c)
        self.assertIn("b.name AS bt", joined_c)
        self.assertIn("b.documentNumber AS dn", joined_c)
        self.assertNotIn("GovAffair.", joined_m + joined_c)
        self.assertNotIn("cross", (joined_m + joined_c).lower())
        self.assertEqual(c["citationId"], "LC-9")

    def test_traverse_cypher_uses_new_labels_and_document_number(self):
        eng = self._engine()
        session = FakeSession(
            lambda q, p: [{"id": "LB-1", "name": "条例", "docNo": "文1",
                           "article": None, "content": None}])
        h = {"step": 2, "action": "traverse", "from": "c1", "relation": "partOf",
             "direction": "out", "bind": "b1", "desc": ""}
        vars_ = {"c1": {"type": "citation", "entities": [{"id": "LC-1", "name": "引用"}]}}
        tr = eng._do_traverse(session, h, vars_)
        q = session.queries[0]
        self.assertIn(":`ZwdmxGJ.LegalCitation`", q)
        self.assertIn(":`ZwdmxGJ.LegalBasis`", q)
        self.assertIn("s.citationId IN $ids", q)          # 起点 id 属性按类型切换
        self.assertIn("o.legalBasisId AS id", q)          # 终点 id 属性按类型切换
        self.assertIn("o.documentNumber AS docNo", q)     # 文号列改名并别名回 docNo
        self.assertNotIn("o.docNo AS docNo", q)
        self.assertNotIn("GovAffair.", q)
        self.assertEqual(tr["bound"][0]["docNo"], "文1")  # 渲染层仍消费 docNo

    def test_vector_search_uses_variant_id_prop(self):
        inst = self._retriever_stub()
        session = FakeSession(lambda q, p: [])
        inst.vector_search(session, self.r.IDX_AFFAIR, [0.0], 5, "事项",
                           id_prop="serviceId")
        self.assertIn("RETURN node.name AS name, node.serviceId AS id", session.queries[0])

    def test_do_locate_binds_service_with_service_id(self):
        calls = {}

        def fake_vs(session, index, vec, k, label, node_where="", id_prop="id"):
            calls["index"] = index
            calls["id_prop"] = id_prop
            return [self.r.Seed(label=label, name="居住证办理", score=0.9, node_id="SVC-1"),
                    self.r.Seed(label=label, name="居住证办理", score=0.88, node_id="SVC-2")]

        eng = self._engine(types.SimpleNamespace(embed=lambda q: [0.0],
                                                 vector_search=fake_vs))
        h = {"step": 1, "action": "locate", "type": "service",
             "query": "居住证", "bind": "a1", "desc": ""}
        tr = eng._do_locate(FakeSession(), h)
        self.assertEqual(calls["id_prop"], "serviceId")
        self.assertEqual(calls["index"], "_zwdmx_g_j_government_service_name_vector_index")
        self.assertEqual(len(tr["bound"]), 1)   # 同名服务只绑 1 个
        self.assertEqual(tr["bound"][0]["id"], "SVC-1")

    def test_validate_plan_accepts_new_and_rejects_cross(self):
        ok = {"hops": [
            {"step": 1, "action": "locate", "type": "事项", "query": "居住证",
             "bind": "a1", "desc": ""},
            {"step": 2, "action": "traverse", "from": "a1", "relation": "所需材料",
             "direction": "out", "bind": "m1", "desc": ""},
            {"step": 3, "action": "traverse", "from": "m1", "relation": "requiresmaterial",
             "direction": "in", "bind": "a2", "desc": ""},
        ]}
        out = self.m.MultiHopEngine._validate_plan(ok, "问题")
        self.assertEqual(out[0]["type"], "service")
        self.assertEqual(out[1]["relation"], "requiresMaterial")
        self.assertEqual(out[2]["relation"], "requiresMaterial")

        bad = {"hops": [
            {"step": 1, "action": "locate", "type": "service", "query": "x",
             "bind": "a1", "desc": ""},
            {"step": 2, "action": "traverse", "from": "a1",
             "relation": "supportCrossRegion", "direction": "out",
             "bind": "x1", "desc": ""},
        ]}
        with self.assertRaises(self.m.PlanError):
            self.m.MultiHopEngine._validate_plan(bad, "问题")

    def test_citation_dedup_key_uses_citation_id(self):
        key = self.r.citation_dedup_key(
            {"citationId": "LC-1", "article": "第一条", "basis": "条例"})
        self.assertEqual(key, "LC-1")
        # citationId 缺失时退回旧语义键（容错）
        self.assertEqual(
            self.r.citation_dedup_key({"article": "第一条", "basis": "条例"}),
            "第一条条例")

    def test_retrieve_citation_dedup_by_citation_id(self):
        inst = self._retriever_stub()

        def rows(query, params):
            idx = params.get("index")
            if idx == self.r.IDX_CITATION_CONTENT:
                # LC-1 与 LC-2 条款/法规相同（article+basis 旧键会撞）；
                # LC-1 再从名称索引重复出现一次
                return [{"name": "条例第一条", "id": "LC-1", "score": 0.9},
                        {"name": "条例第一条", "id": "LC-2", "score": 0.85}]
            if idx == self.r.IDX_CITATION_NAME:
                return [{"name": "条例第一条", "id": "LC-1", "score": 0.7}]
            if "OPTIONAL MATCH" in query:
                return [{"n": "条例第一条", "cid": params["id"], "art": "第一条",
                         "ct": "内容", "bt": "条例", "dn": "文1"}]
            return []

        session = FakeSession(rows)
        inst.driver = FakeDriver(session)
        result = inst.retrieve("办理依据")
        # 共享 citationId 去重：LC-1 重复出现只保留一次；LC-2 与 LC-1 不再按
        # article+basis 误合并 → 共 2 条
        self.assertEqual(len(result.extra_citations), 2)
        self.assertEqual({c["citationId"] for c in result.extra_citations},
                         {"LC-1", "LC-2"})

    def test_prompt_context_renders_new_sections(self):
        result = self.r.RetrievalResult(question="q", affairs=[{
            "name": "居住证办理",
            "legalTimeLimit": "20工作日",
            "departments": ["公安局"],
            "channels": [{"name": "政务大厅", "type": "窗口", "serviceTime": "工作日",
                          "description": ""}],
            "fees": [{"name": "工本费", "status": "收费", "standard": "20元", "basis": ""}],
            "faqs": [{"question": "多久办完？", "answer": "10 个工作日"}],
            "chunks": [{"sourceField": "legalContent", "content": "原文段落"}],
            "materials": ["身份证"],
            "steps": [],
            "citations": [],
        }])
        text = result.to_prompt_context()
        self.assertIn("主管部门：公安局", text)
        self.assertIn("办理渠道（1 个）", text)
        self.assertIn("政务大厅（窗口） 服务时间：工作日", text)
        self.assertIn("收费信息：工本费（收费；标准：20元）", text)
        self.assertIn("常见问答（1 条）", text)
        self.assertIn("问：多久办完？", text)
        self.assertIn("答：10 个工作日", text)
        self.assertIn("原文摘录（1 段）", text)
        self.assertIn("[legalContent] 原文段落", text)
        self.assertIn("法定时限：20工作日", text)
        self.assertNotIn("通办范围", text)


# ---------------------------------------------------------------- 未知取值（已固化：报错）

class UnknownSchemaTests(unittest.TestCase):
    def test_unknown_kg_schema_fails_fast_on_reload(self):
        try:
            with mock.patch.dict(os.environ, {"KG_SCHEMA": "graphrag"}):
                with self.assertRaises(ValueError) as cm:
                    importlib.reload(retriever)
            self.assertIn("KG_SCHEMA", str(cm.exception))
            self.assertIn("govaffair", str(cm.exception))
            self.assertIn("zwdmxgj", str(cm.exception))
        finally:
            _reload(None)  # 恢复默认模块状态，避免影响其他用例

    def test_blankish_unknown_value_also_fails(self):
        try:
            with mock.patch.dict(os.environ, {"KG_SCHEMA": "  "}):
                with self.assertRaises(ValueError):
                    importlib.reload(retriever)
        finally:
            _reload(None)


if __name__ == "__main__":
    unittest.main()
