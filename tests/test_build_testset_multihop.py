#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_testset_multihop.py 单测（不连真实 Neo4j）。

覆盖（桩策略与 tests/test_backfill_vectors.py 相同：无 neo4j 包时装桩模块）：
  1. 候选池 Cypher：标签/关系反引号、2 跳反向模式、rand() 分层、name 长度过滤；
  2. 9 种题型问题模板渲染（与任务模板逐字一致）；
  3. expected_answer 排版：T001 风格材料清单、orderNo 排序、FAQ 前 2 条，
     并用 scripts/score_testset.py 的归一化/F1 验证判分器兼容；
  4. 质量约束：2 跳目标集 3-8（否则重抽）、name 6-40、答案 <800 字符非空；
  5. build_rows：categoryL1 轮转分层、同一事项（按 name）至多 2 题、重抽计数、
     test_id 顺序分配（MH 前缀，不与 T***/P*** 冲突）；
  6. 同名事项去重（优先 serviceObject 非空）；
  7. 端到端 generate（FakeDriver）：默认每型 6 题、总量 ≥50、--seed 复现；
  8. CSV 写出（UTF-8 BOM，12 列表头与现有 testset 逐字一致）与判分器
     load_testsets 互通、ids 输出去重；
  9. CLI 参数解析。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _ensure_neo4j_importable() -> None:
    """无 neo4j 包的环境下装桩（仅测试进程内，不触真实服务）。"""
    try:
        import neo4j  # noqa: F401
        import neo4j.exceptions  # noqa: F401
    except ImportError:
        stub = types.ModuleType("neo4j")
        stub.GraphDatabase = types.SimpleNamespace(driver=lambda *a, **k: None)
        exceptions = types.ModuleType("neo4j.exceptions")

        class _Neo4jError(Exception):
            pass

        class _ServiceUnavailable(_Neo4jError):
            pass

        class _AuthError(_Neo4jError):
            pass

        exceptions.Neo4jError = _Neo4jError
        exceptions.ServiceUnavailable = _ServiceUnavailable
        exceptions.AuthError = _AuthError
        stub.exceptions = exceptions
        sys.modules["neo4j"] = stub
        sys.modules["neo4j.exceptions"] = exceptions


_ensure_neo4j_importable()

from scripts import build_testset_multihop as btm  # noqa: E402
from scripts import score_testset as scorer  # noqa: E402


# ---------------------------------------------------------------- 桩件

class FakeSession:
    """run(query, **params) → handler(query, params) 返回的行列表。"""

    def __init__(self, handler):
        self.handler = handler

    def run(self, query, **params):
        return self.handler(query, params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriver:
    def __init__(self, handler):
        self._handler = handler
        self.database_seen = []

    def session(self, database=None):
        self.database_seen.append(database)
        return FakeSession(self._handler)

    def close(self):
        pass


CATS = ["法人服务", "个人服务"]


def _svc(kind: str, cat: str, i: int, **overrides) -> dict:
    """构造一个合法候选（事项字段 + 题型专属 items/anchorName）。"""
    cand = {
        "serviceId": f"S-{kind}-{CATS.index(cat)}-{i}",
        "name": f"{kind}样板{cat}第{i:02d}号事项",
        "categoryL1": cat,
        "categoryL2": "行政许可",
        "departmentName": "广东省教育厅",
        "serviceObject": "法人",
        "sourceUrl": "https://example.gov.cn/guide",
        "items": _items_for(kind, i),
    }
    if kind in btm.MULTI_HOP_TYPES:
        cand["anchorName"] = {"multi_hop_dept": "广东省教育厅",
                              "multi_hop_material": f"申请表{i:02d}号材料",
                              "multi_hop_legal": f"中华人民共和国教育法第{i}号"}[kind]
    cand.update(overrides)
    return cand


def _items_for(kind: str, i: int):
    if kind == "material":
        return [{"name": "申请表", "required": "是", "orderNo": 2},
                {"name": "营业执照副本", "required": "否", "orderNo": 1}]
    if kind == "department":
        return [{"name": "广东省公安厅", "role": "协同部门"},
                {"name": "广东省教育厅", "role": "主管部门"}]
    if kind == "legal":
        return ["中华人民共和国教育法", "广东省政务服务条例", "中华人民共和国教育法"]
    if kind == "condition":
        return ["符合《教育法》规定的举办者条件", "具备与办学规模相适应的资金"]
    if kind == "process":
        return [{"name": "受理", "orderNo": 2}, {"name": "收件", "orderNo": 1},
                {"name": "决定", "orderNo": 3}]
    if kind == "faq":
        return [{"name": f"第{i}问：如何办理？", "answer": "网上办理即可。", "orderNo": 2},
                {"name": f"第{i}问：收费吗？", "answer": "不收费。", "orderNo": 1},
                {"name": f"第{i}问：要多久？", "answer": "5个工作日。", "orderNo": 3}]
    # 2 跳目标集（4 个，在 3-8 内）
    return [f"反向目标事项零{i}一", "反向目标事项零二", "反向目标事项零三", "反向目标事项零四"]


_KIND_BY_MARKER = {
    "requiresMaterial": "material",
    "handledBy": "department",
    "hasCondition": "condition",
    "hasProcessStep": "process",
    "hasFaq": "faq",
    "citesLegal": "legal",
}


def _pool_kind(query: str) -> str:
    """按 Cypher 文本特征识别查询类型（handler 分发用）。"""
    if "count(*) AS c" in query:
        return "categories"
    if "<-[:`handledBy`]-" in query:
        return "multi_hop_dept"
    if "<-[:`requiresMaterial`]-" in query:
        return "multi_hop_material"
    if "<-[:`partOf`]-" in query:
        return "multi_hop_legal"
    for marker, kind in _KIND_BY_MARKER.items():
        if f"`{marker}`" in query:
            return kind
    raise AssertionError(f"无法识别的查询：{query[:120]}")


def _graph_handler(pool_size: int = 5, per_kind_extra=None):
    """模拟 zwdmxgj 图：每（题型×categoryL1）返回 pool_size 个合法候选。"""
    def handler(query, params):
        kind = _pool_kind(query)
        if kind == "categories":
            return [{"cat": cat, "c": 100 - CATS.index(cat)} for cat in CATS]
        cat = params["cat"]
        assert params["pool"] >= 1
        rows = [_svc(kind, cat, i) for i in range(1, pool_size + 1)]
        if per_kind_extra and (kind, cat) in per_kind_extra:
            rows.extend(per_kind_extra[(kind, cat)])
        return rows
    return handler


def _sink():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------- Cypher 构建


class QueryBuildingTests(unittest.TestCase):
    def test_pool_queries_cover_all_types(self) -> None:
        self.assertEqual(set(btm.POOL_QUERIES), set(btm.QUESTION_TYPES))

    def test_common_filter_and_random_order(self) -> None:
        for key, query in btm.POOL_QUERIES.items():
            with self.subTest(key=key):
                self.assertIn("s.`categoryL1` = $cat", query)
                self.assertIn("size(s.`name`) >= 6", query)
                self.assertIn("size(s.`name`) <= 40", query)
                self.assertIn("ORDER BY rand() LIMIT $pool", query)
                self.assertIn("s.`serviceId` AS serviceId", query)
                if key in btm.MULTI_HOP_TYPES:
                    self.assertIn("anchorName", query)
                else:
                    self.assertNotIn("anchorName", query)

    def test_one_hop_labels_and_relations(self) -> None:
        checks = {
            "material": ("-[r:`requiresMaterial`]->(m:`ZwdmxGJ.Material`)", "r.`required`"),
            "department": ("-[r:`handledBy`]->(d:`ZwdmxGJ.Department`)", "r.`departmentRole`"),
            "condition": "-[:`hasCondition`]->(c:`ZwdmxGJ.ServiceCondition`)",
            "process": "-[r:`hasProcessStep`]->(p:`ZwdmxGJ.ProcessStep`)",
            "faq": "-[r:`hasFaq`]->(f:`ZwdmxGJ.FAQ`)",
        }
        for key, fragments in checks.items():
            with self.subTest(key=key):
                query = btm.POOL_QUERIES[key]
                for fragment in (fragments if isinstance(fragments, tuple) else (fragments,)):
                    self.assertIn(fragment, query)
        # legal：citesLegal→partOf→LegalBasis 两跳链
        legal = btm.POOL_QUERIES["legal"]
        self.assertIn("-[:`citesLegal`]->(:`ZwdmxGJ.LegalCitation`)-[:`partOf`]->(b:`ZwdmxGJ.LegalBasis`)",
                      legal)
        self.assertIn("collect(DISTINCT bn)", legal)

    def test_multi_hop_reverse_patterns_with_lower_bound(self) -> None:
        dept = btm.POOL_QUERIES["multi_hop_dept"]
        self.assertIn("MATCH (d)<-[:`handledBy`]-(o:`ZwdmxGJ.GovernmentService`)", dept)
        self.assertIn("anchorName", dept)
        self.assertIn(f"size(items) >= {btm.HOP_TARGET_MIN}", dept)
        material = btm.POOL_QUERIES["multi_hop_material"]
        self.assertIn("MATCH (m)<-[:`requiresMaterial`]-(o:`ZwdmxGJ.GovernmentService`)", material)
        legal = btm.POOL_QUERIES["multi_hop_legal"]
        self.assertIn(
            "MATCH (b)<-[:`partOf`]-(:`ZwdmxGJ.LegalCitation`)<-[:`citesLegal`]"
            "-(o:`ZwdmxGJ.GovernmentService`)", legal)
        for query in (dept, material, legal):
            self.assertIn("o.`name` <> s.`name`", query)  # 排除锚点事项（含同名跨区县实例）

    def test_categories_query(self) -> None:
        query = btm.categories_query()
        self.assertIn("count(*) AS c", query)
        self.assertIn("ORDER BY c DESC, cat ASC", query)
        self.assertIn("`ZwdmxGJ.GovernmentService`", query)


# ---------------------------------------------------------------- 问题模板


class QuestionTemplateTests(unittest.TestCase):
    def test_all_templates_exact(self) -> None:
        cand = _svc("material", "法人服务", 1)
        cand["name"] = "旅馆业特种行业许可证核发"
        cases = {
            "material": "旅馆业特种行业许可证核发需要提交哪些申请材料？",
            "department": "旅馆业特种行业许可证核发由哪个部门负责办理？",
            "legal": "旅馆业特种行业许可证核发办理依据哪些法规文件？",
            "condition": "办理旅馆业特种行业许可证核发需要满足什么条件？",
            "process": "旅馆业特种行业许可证核发的办理流程包含哪些环节？",
            "faq": "旅馆业特种行业许可证核发的常见问答有哪些？",
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(btm.render_question(key, cand), expected)
        dept_cand = _svc("multi_hop_dept", "法人服务", 1, anchorName="广东省公安厅")
        dept_cand["name"] = "旅馆业特种行业许可证核发"
        self.assertEqual(btm.render_question("multi_hop_dept", dept_cand),
                         "除旅馆业特种行业许可证核发外，广东省公安厅还负责办理哪些事项？")
        mat_cand = _svc("multi_hop_material", "法人服务", 1, anchorName="营业执照")
        mat_cand["name"] = "旅馆业特种行业许可证核发"
        self.assertEqual(btm.render_question("multi_hop_material", mat_cand),
                         "除了旅馆业特种行业许可证核发，还有哪些事项也需要提交营业执照？")
        legal_cand = _svc("multi_hop_legal", "法人服务", 1, anchorName="中华人民共和国教育法")
        self.assertEqual(btm.render_question("multi_hop_legal", legal_cand),
                         "还有哪些事项的办理依据是《中华人民共和国教育法》？")

    def test_unknown_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            btm.render_question("nope", {})
        with self.assertRaises(ValueError):
            btm.render_answer("nope", {})


# ---------------------------------------------------------------- 答案排版


class AnswerRenderingTests(unittest.TestCase):
    def test_material_answer_t001_style(self) -> None:
        cand = _svc("material", "法人服务", 1)
        answer = btm.render_answer("material", cand)
        self.assertEqual(
            answer,
            "必要申请材料：\n"
            "1. 营业执照副本（非必要）\n"     # orderNo=1 排前
            "2. 申请表（必要）",
        )

    def test_required_label_values(self) -> None:
        self.assertEqual(btm.required_label("是"), "（必要）")
        self.assertEqual(btm.required_label(None), "（必要）")  # 缺失默认必要
        self.assertEqual(btm.required_label("否"), "（非必要）")
        self.assertEqual(btm.required_label("false"), "（非必要）")
        self.assertEqual(btm.required_label("0"), "（非必要）")

    def test_department_primary_role_first(self) -> None:
        cand = _svc("department", "法人服务", 1)
        self.assertEqual(btm.render_answer("department", cand),
                         "办理部门：广东省教育厅、广东省公安厅")

    def test_legal_answer_dedups(self) -> None:
        answer = btm.render_answer("legal", _svc("legal", "法人服务", 1))
        self.assertEqual(answer, "主要法律依据：\n1. 中华人民共和国教育法\n2. 广东省政务服务条例")

    def test_condition_answer(self) -> None:
        answer = btm.render_answer("condition", _svc("condition", "法人服务", 1))
        self.assertTrue(answer.startswith("办理条件：\n1. "))

    def test_process_answer_sorted_by_order_no(self) -> None:
        self.assertEqual(
            btm.render_answer("process", _svc("process", "法人服务", 1)),
            "办理步骤：\n1. 收件\n2. 受理\n3. 决定",
        )

    def test_order_no_none_sorts_last(self) -> None:
        cand = _svc("process", "法人服务", 1,
                    items=[{"name": "无序号步骤"}, {"name": "首步", "orderNo": 1}])
        self.assertEqual(btm.render_answer("process", cand),
                         "办理步骤：\n1. 首步\n2. 无序号步骤")

    def test_faq_answer_takes_first_two_by_order_no(self) -> None:
        answer = btm.render_answer("faq", _svc("faq", "法人服务", 1))
        self.assertEqual(
            answer,
            "常见问题：\n"
            "1. 问：第1问：收费吗？\n答：不收费。\n"      # orderNo=1
            "2. 问：第1问：如何办理？\n答：网上办理即可。",  # orderNo=2，第 3 条不取
        )

    def test_faq_collapses_inner_whitespace(self) -> None:
        cand = _svc("faq", "法人服务", 1, items=[
            {"name": "多行答案问", "answer": "第一行\n第二行\t第三段", "orderNo": 1}])
        self.assertIn("答：第一行 第二行 第三段", btm.render_answer("faq", cand))

    def test_multi_hop_answers_numbered_target_list(self) -> None:
        self.assertEqual(
            btm.render_answer("multi_hop_dept", _svc("multi_hop_dept", "法人服务", 3)),
            "广东省教育厅还负责办理以下事项：\n"
            "1. 反向目标事项零3一\n2. 反向目标事项零二\n3. 反向目标事项零三\n4. 反向目标事项零四",
        )
        mat = btm.render_answer("multi_hop_material", _svc("multi_hop_material", "个人服务", 1))
        self.assertTrue(mat.startswith("还需要提交申请表01号材料的事项：\n1. "))
        legal = btm.render_answer("multi_hop_legal", _svc("multi_hop_legal", "个人服务", 2))
        self.assertTrue(legal.startswith("办理依据为《中华人民共和国教育法第2号》的事项：\n1. "))

    def test_empty_items_render_empty(self) -> None:
        for key in btm.QUESTION_TYPES:
            with self.subTest(key=key):
                self.assertEqual(btm.render_answer(key, _svc(key, "法人服务", 1, items=[])), "")


class ScorerCompatTests(unittest.TestCase):
    """expected_answer 排版必须与 scripts/score_testset.py 的归一化兼容。"""

    def test_all_types_survive_normalization(self) -> None:
        for key in btm.QUESTION_TYPES:
            with self.subTest(key=key):
                answer = btm.render_answer(key, _svc(key, "法人服务", 1))
                self.assertTrue(answer.strip())
                normalized = scorer.normalize_text(answer)
                self.assertNotIn("\n", normalized)   # 换行被折叠
                self.assertNotIn("  ", normalized)   # 无多余空白
                self.assertNotEqual(normalized, "")  # 非空答案（不会被判拒答）
                # 空白排版差异不丢分：任意折叠空白形式 F1=1.0、EM=1
                flattened = " ".join(answer.split())
                self.assertEqual(scorer.exact_match(answer, flattened), 1)
                self.assertEqual(scorer.answer_f1(answer, flattened), 1.0)
                # 中文标点（：、。《》？）归一化后不产生空 token 干扰
                self.assertTrue(scorer.tokenize(normalized))

    def test_material_answer_tokens_keep_names_and_flags(self) -> None:
        answer = btm.render_answer("material", _svc("material", "法人服务", 1))
        tokens = scorer.tokenize(scorer.normalize_text(answer))
        for token in ("必要申请材料", "申请表", "营业执照副本", "必要", "非必要"):
            # 中文按单字切分，校验关键词逐字存在
            self.assertTrue(all(ch in tokens for ch in token), token)

    def test_fullwidth_question_marks_unified(self) -> None:
        question = btm.render_question("faq", _svc("faq", "法人服务", 1))
        self.assertEqual(scorer.normalize_text(question).endswith("?"), True)


# ---------------------------------------------------------------- 质量约束


class ConstraintTests(unittest.TestCase):
    def _mh(self, targets, **overrides):
        return _svc("multi_hop_material", "法人服务", 1, items=list(targets), **overrides)

    def test_hop_target_bounds_3_to_8(self) -> None:
        cases = {2: False, 3: True, 5: True, 8: True, 9: False}
        for count, accepted in cases.items():
            with self.subTest(count=count):
                record = btm.build_question(
                    "multi_hop_material", self._mh([f"目标事项{i}号" for i in range(count)]))
                self.assertEqual(record is not None, accepted)

    def test_hop_missing_anchor_rejected(self) -> None:
        cand = self._mh([f"目标事项{i}号" for i in range(4)], anchorName=" ")
        self.assertIsNone(btm.build_question("multi_hop_material", cand))

    def test_name_length_bounds_6_to_40(self) -> None:
        for name, accepted in [("三字名", False), ("六个字的名称", True),
                               ("长" * 40, True), ("长" * 41, False), ("", False)]:
            with self.subTest(name=name[:10], accepted=accepted):
                record = btm.build_question(
                    "material", _svc("material", "法人服务", 1, name=name))
                self.assertEqual(record is not None, accepted)

    def test_missing_service_id_rejected(self) -> None:
        self.assertIsNone(btm.build_question(
            "material", _svc("material", "法人服务", 1, serviceId=None)))

    def test_answer_over_800_chars_rejected(self) -> None:
        long_items = [{"name": "超长材料名称" + "x" * 100, "required": "是"}] * 8
        cand = _svc("material", "法人服务", 1, items=long_items)
        answer = btm.render_answer("material", cand)
        self.assertGreaterEqual(len(answer), btm.ANSWER_MAX_CHARS)
        self.assertIsNone(btm.build_question("material", cand))

    def test_faq_long_answer_rejected(self) -> None:
        cand = _svc("faq", "法人服务", 1, items=[
            {"name": "问", "answer": "长" * 900, "orderNo": 1}])
        self.assertIsNone(btm.build_question("faq", cand))

    def test_empty_answer_rejected(self) -> None:
        self.assertIsNone(btm.build_question("legal", _svc("legal", "法人服务", 1, items=[" ", ""])))
        self.assertIsNone(btm.build_question(
            "department", _svc("department", "法人服务", 1,
                               items=[{"name": None, "role": "主管部门"}])))

    def test_record_fields_complete(self) -> None:
        record = btm.build_question("material", _svc("material", "法人服务", 1))
        self.assertEqual(record["doc_id"], "service:S-material-0-1")
        self.assertEqual(record["title"], record["question"].split("需要")[0])
        self.assertEqual(record["category_l2"], "行政许可")
        self.assertEqual(record["answer_type"], "materials")
        self.assertEqual(record["source_tables"], "requiresMaterial|Material")
        self.assertEqual(record["source_url"], "https://example.gov.cn/guide")

    def test_department_name_fallback_to_anchor(self) -> None:
        record = btm.build_question(
            "multi_hop_dept", _svc("multi_hop_dept", "法人服务", 1, departmentName=" "))
        self.assertEqual(record["department_name"], "广东省教育厅")
        record2 = btm.build_question(
            "department", _svc("department", "法人服务", 1, departmentName=" "))
        self.assertEqual(record2["department_name"], "广东省教育厅")


# ---------------------------------------------------------------- build_rows


class BuildRowsTests(unittest.TestCase):
    def test_per_type_quota_and_sequential_test_ids(self) -> None:
        pools = {("material", "法人服务"): [_svc("material", "法人服务", i) for i in range(1, 4)]}
        rows, stats = btm.build_rows(pools, ["法人服务"], per_type=2)
        self.assertEqual([r["test_id"] for r in rows], ["MH001", "MH002"])
        self.assertEqual(stats["per_type"]["material"], 2)
        self.assertEqual(sum(stats["per_type"].values()), 2)

    def test_stratification_round_robin_over_categories(self) -> None:
        pools = {(t, c): [_svc(t, c, i) for i in range(1, 3)]
                 for t in ("material", "department") for c in CATS}
        rows, stats = btm.build_rows(pools, CATS, per_type=4)
        material_rows = [r for r in rows if r["answer_type"] == "materials"]
        self.assertEqual([r["category_l1"] for r in material_rows],
                         ["法人服务", "个人服务", "法人服务", "个人服务"])
        self.assertEqual(stats["per_type"]["department"], 4)

    def test_resample_counted_when_candidates_invalid(self) -> None:
        bad = _svc("multi_hop_material", "法人服务", 1, items=["仅两个", "目标而已"])
        good = _svc("multi_hop_material", "法人服务", 2)
        pools = {("multi_hop_material", "法人服务"): [bad, good, dict(good)]}
        rows, stats = btm.build_rows(pools, ["法人服务"], per_type=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats["resampled"]["multi_hop_material"], 1)

    def test_same_service_at_most_two_questions(self) -> None:
        # 同一事项（按 name，含同名跨区县不同 serviceId 实例）至多 2 道题
        shared = _svc("material", "法人服务", 1)
        twin = dict(shared, serviceId="S-twin-district")   # 同名另一实例
        other = _svc("legal", "法人服务", 9)
        pools = {
            ("material", "法人服务"): [shared],
            ("department", "法人服务"): [shared],
            ("legal", "法人服务"): [shared, twin, other],
        }
        rows, stats = btm.build_rows(pools, ["法人服务"], per_type=1)
        self.assertEqual([r["title"] for r in rows],
                         [shared["name"], shared["name"], other["name"]])
        self.assertEqual(stats["skipped_used"]["legal"], 2)
        self.assertEqual(stats["per_type"]["legal"], 1)

    def test_exhausted_pools_yield_short_quotas(self) -> None:
        pools = {("material", "法人服务"): [_svc("material", "法人服务", 1)]}
        rows, stats = btm.build_rows(pools, ["法人服务", "个人服务"], per_type=3)
        self.assertEqual(len(rows), 1)   # 只有 material 1 题，其余题型池空
        self.assertEqual(stats["per_type"]["material"], 1)
        self.assertEqual(stats["per_type"]["faq"], 0)

    def test_empty_pools_no_crash(self) -> None:
        rows, stats = btm.build_rows({}, ["法人服务"], per_type=3)
        self.assertEqual(rows, [])
        self.assertEqual(sum(stats["per_type"].values()), 0)


class DedupePoolTests(unittest.TestCase):
    def test_dedup_by_name_prefers_service_object(self) -> None:
        rows = [
            _svc("material", "法人服务", 1, serviceId="S-first", serviceObject=""),
            _svc("material", "法人服务", 2, name="material样板法人服务第01号事项",
                 serviceId="S-second", serviceObject="法人"),
            _svc("material", "法人服务", 3, serviceId="S-other"),
            _svc("material", "法人服务", 4, name="  "),
        ]
        pool = btm.dedupe_pool(rows)
        self.assertEqual([r["serviceId"] for r in pool], ["S-second", "S-other"])


# ---------------------------------------------------------------- 端到端（FakeDriver）


class GenerateTests(unittest.TestCase):
    def test_default_quota_total_at_least_50(self) -> None:
        driver = FakeDriver(_graph_handler(pool_size=5))
        rows, stats = btm.generate(driver, per_type=6, seed=1, log=lambda _: None)
        self.assertGreaterEqual(len(rows), 50)
        self.assertEqual(len(rows), 6 * len(btm.QUESTION_TYPES))
        self.assertTrue(all(stats["per_type"][t] == 6 for t in btm.QUESTION_TYPES))
        # 连的是 zwdmxgj 库
        self.assertEqual(driver.database_seen, [btm.NEO4J_DB])

    def test_seed_reproducible(self) -> None:
        rows_a, _ = btm.generate(FakeDriver(_graph_handler()), per_type=4, seed=7,
                                 log=lambda _: None)
        rows_b, _ = btm.generate(FakeDriver(_graph_handler()), per_type=4, seed=7,
                                 log=lambda _: None)
        self.assertEqual(rows_a, rows_b)
        self.assertEqual([r["test_id"] for r in rows_a],
                         [r["test_id"] for r in rows_b])

    def test_different_seed_still_valid_and_same_quota(self) -> None:
        rows, stats = btm.generate(FakeDriver(_graph_handler()), per_type=2, seed=99,
                                   log=lambda _: None)
        self.assertEqual(sum(stats["per_type"].values()), len(rows))
        ids = [r["test_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_categories_raises(self) -> None:
        driver = FakeDriver(lambda query, params: [])
        with self.assertRaises(RuntimeError):
            btm.generate(driver, per_type=1, log=lambda _: None)


# ---------------------------------------------------------------- 输出文件


class OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _rows(self):
        return btm.generate(FakeDriver(_graph_handler()), per_type=6, seed=3,
                            log=lambda _: None)[0]

    def test_csv_has_bom_and_exact_header(self) -> None:
        rows = self._rows()
        path = self.dir / "testset_multihop.csv"
        btm.write_csv(rows, path)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))       # UTF-8 BOM
        text = raw.decode("utf-8-sig")
        header = text.splitlines()[0]
        self.assertEqual(header, ",".join(btm.CSV_COLUMNS))
        # 与 data/pilot、data/personal 现有表头逐字一致（实测为 category_l2、12 列）
        self.assertEqual(btm.CSV_COLUMNS, (
            "test_id", "question", "expected_answer", "doc_id", "title",
            "category_l1", "category_l2", "service_id", "department_name",
            "answer_type", "source_url", "source_tables"))
        with path.open(encoding="utf-8-sig", newline="") as fh:
            records = list(csv.DictReader(fh))
        self.assertEqual(len(records), len(rows))
        for record, row in zip(records, rows):
            self.assertEqual(record["test_id"], row["test_id"])
            self.assertEqual(record["question"], row["question"])
            self.assertEqual(record["expected_answer"], row["expected_answer"])
            self.assertEqual(record["answer_type"], row["answer_type"])

    def test_csv_readable_by_scorer_and_answer_types_expected(self) -> None:
        path = self.dir / "testset_multihop.csv"
        btm.write_csv(self._rows(), path)
        loaded = scorer.load_testsets([path])   # 判分器能读、无重复 test_id
        self.assertEqual(len(loaded), 6 * len(btm.QUESTION_TYPES))
        self.assertEqual({r["answer_type"] for r in loaded},
                         {"materials", "department", "legal_basis", "condition",
                          "process", "multi_hop_dept", "multi_hop_material",
                          "multi_hop_legal", "faq"})
        ids = {r["test_id"] for r in loaded}
        self.assertFalse(ids & {"T001", "P001"})   # 不与现有两域 test_id 冲突

    def test_perfect_prediction_scores_full_em_and_f1(self) -> None:
        # 用题集自身答案当 mock 预测走判分器：排版与归一化完全兼容 → EM/F1 全满
        path = self.dir / "testset_multihop.csv"
        btm.write_csv(self._rows(), path)
        loaded = scorer.load_testsets([path])
        predictions = {r["test_id"]: r["expected_answer"] for r in loaded}
        items = scorer.score_rows(loaded, predictions)
        self.assertEqual(len(items), len(loaded))
        self.assertTrue(all(item["answered"] for item in items))       # 无拒答/未答
        self.assertTrue(all(item["em"] == 1 for item in items))
        self.assertTrue(all(item["f1"] == 1.0 for item in items))
        summary = scorer.group_summary(items)
        self.assertEqual((summary["em"], summary["f1"]), (1.0, 1.0))

    def test_ids_out_dedups_preserving_order(self) -> None:
        rows = self._rows()
        path = self.dir / "ids.txt"
        count = btm.write_ids(rows, path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), count)
        self.assertEqual(len(lines), len(set(lines)))
        expected: list[str] = []
        for row in rows:
            if row["service_id"] not in expected:
                expected.append(row["service_id"])
        self.assertEqual(lines, expected)


# ---------------------------------------------------------------- CLI


class ParseArgsTests(unittest.TestCase):
    def test_defaults_and_derived_ids_out(self) -> None:
        args = btm.parse_args(["--out", "out/testset_multihop.csv"])
        self.assertEqual(args.per_type, 6)
        self.assertEqual(args.pool_per_cat, btm.DEFAULT_POOL_PER_CAT)
        self.assertEqual(args.seed, btm.DEFAULT_SEED)
        self.assertEqual(args.ids_out, str(Path("out/testset_multihop.csv").with_suffix(""))
                         + "_service_ids.txt")

    def test_explicit_options(self) -> None:
        args = btm.parse_args(["--out", "a.csv", "--ids-out", "ids.txt",
                               "--per-type", "3", "--seed", "9",
                               "--pool-per-cat", "15"])
        self.assertEqual((args.per_type, args.seed, args.pool_per_cat,
                          args.ids_out), (3, 9, 15, "ids.txt"))

    def test_invalid_values_exit(self) -> None:
        with redirect_stderr(io.StringIO()):
            for argv in (["--out", "a.csv", "--per-type", "0"],
                         ["--out", "a.csv", "--pool-per-cat", "0"]):
                with self.assertRaises(SystemExit) as ctx:
                    btm.parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_out_is_required(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                btm.parse_args([])


if __name__ == "__main__":
    unittest.main()
