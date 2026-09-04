#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 zwdmxgj 图生成多跳评测题集（模板法，不调 LLM）。

背景
----
data/pilot、data/personal 现有 testset.csv 只覆盖单事项单关系问答；本脚本从
zwdmxgj 图（OpenSPG 导入）按模板批量生成 9 种题型的结构化评测题，其中 3 种为
反向 2 跳题（multi_hop_*），用于评测 KAG 式多跳检索（qa/multihop.py）。

图事实（schemas/ZwdmxGJ-v0.3.schema、scripts/backfill_vectors.py 实测）：
  * 标签带点前缀，Cypher 中必须反引号：`ZwdmxGJ.GovernmentService` 等；
  * id 属性：serviceId / materialId / citationId / legalBasisId / conditionId / faqId；
  * 关系用短名：requiresMaterial(required, orderNo) / handledBy(departmentRole) /
    citesLegal(orderNo) / partOf / hasCondition / hasProcessStep(orderNo) /
    hasFaq(orderNo)；
  * 同名事项跨区县存在多实例：抽样按 name 去重，且优先选 serviceObject 非空的。

题型（--per-type 控制每型题数，默认 6，9 型共 54 题 ≥50）
------------------------------------------------------
  型                  问题模板                                      answer_type
  material            {事项}需要提交哪些申请材料？                   materials（沿用现有值域）
  department          {事项}由哪个部门负责办理？                     department（沿用）
  legal               {事项}办理依据哪些法规文件？                   legal_basis（沿用）
  condition           办理{事项}需要满足什么条件？                   condition（沿用）
  process             {事项}的办理流程包含哪些环节？                 process（沿用）
  multi_hop_dept      除{事项}外，{部门}还负责办理哪些事项？         multi_hop_dept（新增）
  multi_hop_material  除了{事项}，还有哪些事项也需要提交{材料}？     multi_hop_material（新增）
  multi_hop_legal     还有哪些事项的办理依据是《{法规}》？           multi_hop_legal（新增）
  faq                 {事项}的常见问答有哪些？                       faq（沿用）

answer_type 值域：materials/department/legal_basis/condition/process/faq 均为
data/pilot、data/personal 两份 testset.csv 中已出现的取值；multi_hop_dept /
multi_hop_material / multi_hop_legal 为本脚本新增的简短英文标签（判分器
scripts/score_testset.py 按 answer_type 原样分组，不做枚举校验）。

质量控制
--------
  * 分层抽样：categoryL1 × 题型轮转（round-robin）；候选池在图侧 ORDER BY rand()
    随机抽取，再由 random.Random(--seed) 洗牌后顺序选用（seed 复现 Python 侧选择，
    图侧 rand() 池本身随每次运行变化）；
  * 事项 name 长度 6-40 字；同一事项（按 name，覆盖同名跨区县实例）至多 2 道题；
  * 2 跳题目标集 3-8 个，否则重抽；expected_answer 非空且 <800 字符；
  * expected_answer 排版对齐判分器归一化（score_testset.py 的 NFKC + 中文标点映射
    + 空白折叠：换行/编号在判分时不影响 EM），materials 型参考现有 T001 风格
    「必要申请材料：\n1. xxx（必要）」。

输出
----
  --out <csv>     testset.csv（UTF-8 BOM，12 列，列名与 data/pilot/testset.csv 实际
                  表头逐字一致，test_id 前缀 MH，不与现有 T***/P*** 冲突）
  --ids-out <txt> 去重 serviceId 每行一个（供 backfill_vectors.py --service-ids 回填向量）

用法示例
--------
  python scripts/build_testset_multihop.py \
      --out data/pilot/testset_multihop.csv \
      --ids-out data/pilot/testset_multihop_service_ids.txt --per-type 6 --seed 42

环境变量：NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE（默认
bolt://127.0.0.1:7687 / neo4j / neo4j@openspg / zwdmxgj，读取方式与
scripts/backfill_vectors.py 一致）。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence

from neo4j import GraphDatabase

try:  # 测试桩环境下 neo4j.exceptions 可能不存在，见 tests/test_build_testset_multihop.py
    from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable

    _NEO4J_FATAL = (Neo4jError, ServiceUnavailable, AuthError)
except ImportError:  # pragma: no cover
    _NEO4J_FATAL = ()

# ---------------------------------------------------------------- 配置

REPO_ROOT = Path(__file__).resolve().parents[1]

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j@openspg")
NEO4J_DB = os.environ.get("NEO4J_DATABASE", "zwdmxgj")

SERVICE_LABEL = "ZwdmxGJ.GovernmentService"
DEPARTMENT_LABEL = "ZwdmxGJ.Department"
MATERIAL_LABEL = "ZwdmxGJ.Material"
CITATION_LABEL = "ZwdmxGJ.LegalCitation"
BASIS_LABEL = "ZwdmxGJ.LegalBasis"
CONDITION_LABEL = "ZwdmxGJ.ServiceCondition"
STEP_LABEL = "ZwdmxGJ.ProcessStep"
FAQ_LABEL = "ZwdmxGJ.FAQ"

# 题型（固定顺序 = test_id 分配顺序）
QUESTION_TYPES: tuple[str, ...] = (
    "material", "department", "legal", "condition", "process",
    "multi_hop_dept", "multi_hop_material", "multi_hop_legal", "faq",
)

# 现有 testset.csv 已有取值沿用；2 跳题型为新增英文标签
ANSWER_TYPE: dict[str, str] = {
    "material": "materials",          # pilot T001 / personal P001
    "department": "department",       # personal P010
    "legal": "legal_basis",           # pilot T005 / personal P007
    "condition": "condition",         # personal P002
    "process": "process",             # pilot T003 / personal P003
    "multi_hop_dept": "multi_hop_dept",        # 新增
    "multi_hop_material": "multi_hop_material",  # 新增
    "multi_hop_legal": "multi_hop_legal",      # 新增
    "faq": "faq",                     # personal P008
}

MULTI_HOP_TYPES = frozenset({"multi_hop_dept", "multi_hop_material", "multi_hop_legal"})

# source_tables：构成该题的关系|标签链（短名，沿用图上边名）
SOURCE_TABLES: dict[str, str] = {
    "material": "requiresMaterial|Material",
    "department": "handledBy|Department",
    "legal": "citesLegal|LegalCitation|partOf|LegalBasis",
    "condition": "hasCondition|ServiceCondition",
    "process": "hasProcessStep|ProcessStep",
    "multi_hop_dept": "handledBy|Department|GovernmentService",
    "multi_hop_material": "requiresMaterial|Material|GovernmentService",
    "multi_hop_legal": "citesLegal|LegalCitation|partOf|LegalBasis|GovernmentService",
    "faq": "hasFaq|FAQ",
}

CSV_COLUMNS: tuple[str, ...] = (
    "test_id", "question", "expected_answer", "doc_id", "title",
    "category_l1", "category_l2", "service_id", "department_name",
    "answer_type", "source_url", "source_tables",
)

TEST_ID_PREFIX = "MH"   # 与 pilot T*** / personal P*** 不冲突（判分器拒绝重复 test_id）

# 抽样与答案约束
NAME_MIN, NAME_MAX = 6, 40
HOP_TARGET_MIN, HOP_TARGET_MAX = 3, 8
MAX_USES_PER_SERVICE = 2
ANSWER_MAX_CHARS = 800
FAQ_PAIRS_IN_ANSWER = 2

DEFAULT_SEED = 42
DEFAULT_POOL_PER_CAT = 40   # 每个（题型 × categoryL1）候选池大小

# ---------------------------------------------------------------- Cypher

# 事项节点投影（doc_id 沿用 pilot 的 service:{serviceId} 约定，在 Python 侧拼）
_SERVICE_PROJECTION = (
    "s.`serviceId` AS serviceId, s.`name` AS name, s.`categoryL1` AS categoryL1, "
    "s.`categoryL2` AS categoryL2, s.`departmentName` AS departmentName, "
    "s.`serviceObject` AS serviceObject, s.`sourceUrl` AS sourceUrl"
)

# 公共过滤：分类分层 + name 长度 6-40 + id 非空
_SERVICE_FILTER = (
    "s.`categoryL1` = $cat AND s.`serviceId` IS NOT NULL AND s.`name` IS NOT NULL "
    f"AND size(s.`name`) >= {NAME_MIN} AND size(s.`name`) <= {NAME_MAX}"
)


def categories_query() -> str:
    """categoryL1 清单（带量级，按量降序、名称升序保证确定性轮转顺序）。"""
    return (
        f"MATCH (s:`{SERVICE_LABEL}`) "
        "WHERE s.`categoryL1` IS NOT NULL AND s.`categoryL1` <> '' "
        "RETURN s.`categoryL1` AS cat, count(*) AS c "
        "ORDER BY c DESC, cat ASC"
    )


def _pool_return(items_expr: str, extra: str = "") -> str:
    tail = f", {extra}" if extra else ""
    return f"RETURN {_SERVICE_PROJECTION}, {items_expr} AS items{tail} ORDER BY rand() LIMIT $pool"


# 每题型一个候选池查询：返回事项字段 + items（题型专属负载数据）。
# 1 跳型用 MATCH 关系天然过滤无边事项；2 跳型反向扩展并预筛目标数 >= HOP_TARGET_MIN，
# 上界（<= HOP_TARGET_MAX）由 Python 侧校验后决定重抽。
POOL_QUERIES: dict[str, str] = {
    # 材料：requiresMaterial 边（required/orderNo）+ Material.name
    "material": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[r:`requiresMaterial`]->(m:`{MATERIAL_LABEL}`) "
        "WITH s, {name: m.`name`, required: r.`required`, orderNo: r.`orderNo`} AS mi "
        "WITH s, collect(mi) AS items "
        + _pool_return("items")
    ),
    # 部门：handledBy（departmentRole=主管部门优先排序在渲染时做）
    "department": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[r:`handledBy`]->(d:`{DEPARTMENT_LABEL}`) "
        "WITH s, {name: d.`name`, role: r.`departmentRole`} AS di "
        "WITH s, collect(di) AS items "
        + _pool_return("items")
    ),
    # 法规：citesLegal→partOf→LegalBasis.name 去重清单
    "legal": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[:`citesLegal`]->(:`{CITATION_LABEL}`)-[:`partOf`]->(b:`{BASIS_LABEL}`) "
        "WITH s, b.`name` AS bn "
        "WITH s, collect(DISTINCT bn) AS items "
        + _pool_return("items")
    ),
    # 条件：hasCondition→ServiceCondition.name（即条件原文）
    "condition": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[:`hasCondition`]->(c:`{CONDITION_LABEL}`) "
        "WITH s, c.`name` AS cn "
        "WITH s, collect(cn) AS items "
        + _pool_return("items")
    ),
    # 流程：hasProcessStep 边 orderNo 排序（排序在渲染时做）+ ProcessStep.name
    "process": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[r:`hasProcessStep`]->(p:`{STEP_LABEL}`) "
        "WITH s, {name: p.`name`, orderNo: r.`orderNo`} AS pi "
        "WITH s, collect(pi) AS items "
        + _pool_return("items")
    ),
    # 常见问答：hasFaq→FAQ.name+answer（答案取前 2 条，选取在渲染时做）
    "faq": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[r:`hasFaq`]->(f:`{FAQ_LABEL}`) "
        "WITH s, {name: f.`name`, answer: f.`answer`, orderNo: r.`orderNo`} AS fi "
        "WITH s, collect(fi) AS items "
        + _pool_return("items")
    ),
    # 2 跳·部门：{部门} 还负责办理哪些事项（反向 handledBy，排除同名事项）。
    # 锚点度预筛 <= HOP_TARGET_MAX：避免热门部门（管数万事项）让 collect 爆炸，
    # 且冷门锚点天然满足目标集上界，配合 >= HOP_TARGET_MIN 构成 3-8 区间。
    "multi_hop_dept": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[:`handledBy`]->(d:`{DEPARTMENT_LABEL}`) "
        f"WHERE COUNT {{ (d)<-[:`handledBy`]-() }} <= {HOP_TARGET_MAX} "
        f"MATCH (d)<-[:`handledBy`]-(o:`{SERVICE_LABEL}`) "
        "WHERE o.`name` IS NOT NULL AND o.`name` <> s.`name` "
        "WITH s, d.`name` AS anchorName, collect(DISTINCT o.`name`) AS items "
        f"WHERE size(items) >= {HOP_TARGET_MIN} "
        + _pool_return("items", "anchorName")
    ),
    # 2 跳·材料：还有哪些事项也需要提交{材料}（反向 requiresMaterial）
    "multi_hop_material": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[:`requiresMaterial`]->(m:`{MATERIAL_LABEL}`) "
        f"WHERE COUNT {{ (m)<-[:`requiresMaterial`]-() }} <= {HOP_TARGET_MAX} "
        f"MATCH (m)<-[:`requiresMaterial`]-(o:`{SERVICE_LABEL}`) "
        "WHERE o.`name` IS NOT NULL AND o.`name` <> s.`name` "
        "WITH s, m.`name` AS anchorName, collect(DISTINCT o.`name`) AS items "
        f"WHERE size(items) >= {HOP_TARGET_MIN} "
        + _pool_return("items", "anchorName")
    ),
    # 2 跳·法规：还有哪些事项的办理依据是《{法规}》（LegalBasis 反向 partOf+citesLegal）。
    # 锚点度用 citation 数做代理上界：事项数 <= 引用数（同一事项同法规多引用被 DISTINCT 去重）。
    "multi_hop_legal": (
        f"MATCH (s:`{SERVICE_LABEL}`) WHERE {_SERVICE_FILTER} "
        f"MATCH (s)-[:`citesLegal`]->(:`{CITATION_LABEL}`)-[:`partOf`]->(b:`{BASIS_LABEL}`) "
        f"WHERE COUNT {{ (b)<-[:`partOf`]-(:`{CITATION_LABEL}`) }} <= {HOP_TARGET_MAX} "
        f"MATCH (b)<-[:`partOf`]-(:`{CITATION_LABEL}`)<-[:`citesLegal`]-(o:`{SERVICE_LABEL}`) "
        "WHERE o.`name` IS NOT NULL AND o.`name` <> s.`name` "
        "WITH s, b.`name` AS anchorName, collect(DISTINCT o.`name`) AS items "
        f"WHERE size(items) >= {HOP_TARGET_MIN} "
        + _pool_return("items", "anchorName")
    ),
}


# ---------------------------------------------------------------- 渲染工具


def _clean(value: Any) -> str:
    """None 安全的 str + strip；非 str 先 str()。"""
    if value is None:
        return ""
    return str(value).strip()


def _order_key(item: dict) -> tuple[bool, int]:
    """orderNo 升序（None/非法排最后，Python sorted 稳定保留原始顺序）。"""
    try:
        n = int(item.get("orderNo"))
    except (TypeError, ValueError):
        return (True, 0)
    return (False, n)


def _numbered(lines: Sequence[str]) -> str:
    return "\n".join(f"{i}. {text}" for i, text in enumerate(lines, 1))


def _dedup_keep_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = _clean(v)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def required_label(required: Any) -> str:
    """requiresMaterial.required（Text）→（必要）/（非必要）；未知/缺失默认必要。"""
    v = _clean(required)
    if v in {"否", "非必要", "非必需", "false", "False", "FALSE", "0", "no", "No"}:
        return "（非必要）"
    return "（必要）"


def dept_names_in_order(items: Sequence[dict]) -> list[str]:
    """部门清单：departmentRole 含“主管”的排最前，去重保序。"""
    entries = [(_clean(i.get("role")), _clean(i.get("name")))
               for i in items if isinstance(i, dict)]
    ordered = [n for r, n in entries if "主管" in r] + \
              [n for r, n in entries if "主管" not in r]
    return _dedup_keep_order(ordered)


def render_question(type_key: str, cand: dict) -> str:
    name = _clean(cand.get("name"))
    if type_key == "material":
        return f"{name}需要提交哪些申请材料？"
    if type_key == "department":
        return f"{name}由哪个部门负责办理？"
    if type_key == "legal":
        return f"{name}办理依据哪些法规文件？"
    if type_key == "condition":
        return f"办理{name}需要满足什么条件？"
    if type_key == "process":
        return f"{name}的办理流程包含哪些环节？"
    if type_key == "multi_hop_dept":
        return f"除{name}外，{_clean(cand.get('anchorName'))}还负责办理哪些事项？"
    if type_key == "multi_hop_material":
        return f"除了{name}，还有哪些事项也需要提交{_clean(cand.get('anchorName'))}？"
    if type_key == "multi_hop_legal":
        return f"还有哪些事项的办理依据是《{_clean(cand.get('anchorName'))}》？"
    if type_key == "faq":
        return f"{name}的常见问答有哪些？"
    raise ValueError(f"未知题型：{type_key}")


def render_answer(type_key: str, cand: dict) -> str:
    """由 items/anchorName 渲染 expected_answer；可能返回空串（由调用方重抽）。"""
    items = cand.get("items") or []
    if type_key == "material":
        rows = [i for i in items if isinstance(i, dict) and _clean(i.get("name"))]
        rows.sort(key=_order_key)
        if not rows:
            return ""
        return "必要申请材料：\n" + _numbered(
            [f"{_clean(i.get('name'))}{required_label(i.get('required'))}" for i in rows])
    if type_key == "department":
        names = dept_names_in_order(items)
        if not names:
            return ""
        return "办理部门：" + "、".join(names)
    if type_key == "legal":
        names = _dedup_keep_order(items)
        if not names:
            return ""
        return "主要法律依据：\n" + _numbered(names)
    if type_key == "condition":
        names = _dedup_keep_order(items)
        if not names:
            return ""
        return "办理条件：\n" + _numbered(names)
    if type_key == "process":
        rows = [i for i in items if isinstance(i, dict) and _clean(i.get("name"))]
        rows.sort(key=_order_key)
        if not rows:
            return ""
        return "办理步骤：\n" + _numbered([_clean(i.get("name")) for i in rows])
    if type_key in MULTI_HOP_TYPES:
        anchor = _clean(cand.get("anchorName"))
        targets = _dedup_keep_order(items)
        if not anchor or not targets:
            return ""
        if type_key == "multi_hop_dept":
            header = f"{anchor}还负责办理以下事项："
        elif type_key == "multi_hop_material":
            header = f"还需要提交{anchor}的事项："
        else:
            header = f"办理依据为《{anchor}》的事项："
        return header + "\n" + _numbered(targets)
    if type_key == "faq":
        rows = [i for i in items if isinstance(i, dict)
                and _clean(i.get("name")) and _clean(i.get("answer"))]
        rows.sort(key=_order_key)
        if not rows:
            return ""
        lines: list[str] = []
        for i, row in enumerate(rows[:FAQ_PAIRS_IN_ANSWER], 1):
            # 答案内部空白折叠为单空格（判分器同样折叠空白，排版不影响得分）
            answer = " ".join(_clean(row.get("answer")).split())
            lines.append(f"{i}. 问：{_clean(row.get('name'))}")
            lines.append(f"答：{answer}")
        return "常见问题：\n" + "\n".join(lines)
    raise ValueError(f"未知题型：{type_key}")


def department_display(cand: dict, type_key: str) -> str:
    """department_name 列：优先事项节点的 departmentName，缺失时回退题面部门名。"""
    dept = _clean(cand.get("departmentName"))
    if dept:
        return dept
    if type_key == "multi_hop_dept":
        return _clean(cand.get("anchorName"))
    if type_key == "department":
        names = dept_names_in_order(cand.get("items") or [])
        return names[0] if names else ""
    return ""


# ---------------------------------------------------------------- 题目构建


def build_question(type_key: str, cand: dict) -> dict | None:
    """校验单个候选并渲染一行完整题目记录；不满足约束返回 None（调用方重抽）。"""
    name = _clean(cand.get("name"))
    service_id = _clean(cand.get("serviceId"))
    if not name or not service_id:
        return None
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        return None
    if type_key in MULTI_HOP_TYPES:
        anchor = _clean(cand.get("anchorName"))
        if not anchor:
            return None
        targets = _dedup_keep_order(cand.get("items") or [])
        if not (HOP_TARGET_MIN <= len(targets) <= HOP_TARGET_MAX):
            return None
    answer = render_answer(type_key, cand)
    if not answer.strip() or len(answer) >= ANSWER_MAX_CHARS:
        return None
    return {
        "question": render_question(type_key, cand),
        "expected_answer": answer,
        "doc_id": f"service:{service_id}",
        "title": name,
        "category_l1": _clean(cand.get("categoryL1")),
        "category_l2": _clean(cand.get("categoryL2")),
        "service_id": service_id,
        "department_name": department_display(cand, type_key),
        "answer_type": ANSWER_TYPE[type_key],
        "source_url": _clean(cand.get("sourceUrl")),
        "source_tables": SOURCE_TABLES[type_key],
    }


def dedupe_pool(rows: Sequence[dict]) -> list[dict]:
    """同名事项跨区县多实例：按 name 去重，优先保留 serviceObject 非空的。"""
    best: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = _clean(row.get("name"))
        if not key:
            continue
        if key not in best:
            best[key] = row
            order.append(key)
        elif _clean(row.get("serviceObject")) and not _clean(best[key].get("serviceObject")):
            best[key] = row
    return [best[k] for k in order]


def build_rows(
    pools: dict[tuple[str, str], list[dict]],
    categories: Sequence[str],
    per_type: int,
    types: Sequence[str] = QUESTION_TYPES,
) -> tuple[list[dict], dict[str, Any]]:
    """从候选池选题：题型顺序 × categoryL1 轮转分层，直至每型满 per_type 或池尽。

    返回 (rows, stats)；rows 已含顺序分配的 test_id（MH001 起）。约束不过的候选
    计入重抽（stats.resampled），同一事项（按 name）超过 MAX_USES_PER_SERVICE 次
    计入 stats.skipped_used。
    """
    stats: dict[str, Any] = {
        "per_type": {t: 0 for t in QUESTION_TYPES},
        "resampled": {t: 0 for t in QUESTION_TYPES},
        "skipped_used": {t: 0 for t in QUESTION_TYPES},
    }
    rows: list[dict] = []
    used: Counter[str] = Counter()
    for type_key in types:
        cursor = {cat: 0 for cat in categories}
        remaining = per_type
        while remaining > 0:
            added = False
            for cat in categories:
                if remaining <= 0:
                    break
                pool = pools.get((type_key, cat), [])
                while cursor[cat] < len(pool):
                    cand = pool[cursor[cat]]
                    cursor[cat] += 1
                    if used[_clean(cand.get("name"))] >= MAX_USES_PER_SERVICE:
                        stats["skipped_used"][type_key] += 1
                        continue
                    record = build_question(type_key, cand)
                    if record is None:
                        stats["resampled"][type_key] += 1
                        continue
                    record["test_id"] = f"{TEST_ID_PREFIX}{len(rows) + 1:03d}"
                    rows.append(record)
                    stats["per_type"][type_key] += 1
                    used[record["title"]] += 1
                    remaining -= 1
                    added = True
                    break
            if not added:
                break   # 所有类目的池已耗尽
    return rows, stats


# ---------------------------------------------------------------- 主流程


def fetch_categories(session) -> list[str]:
    rows = list(session.run(categories_query()))
    return [_clean(r.get("cat")) for r in rows if _clean(r.get("cat"))]


def fetch_pools(
    session,
    categories: Sequence[str],
    rng: random.Random,
    pool_per_cat: int = DEFAULT_POOL_PER_CAT,
    types: Sequence[str] = QUESTION_TYPES,
    log: Callable[[str], None] = print,
) -> dict[tuple[str, str], list[dict]]:
    """按（题型 × categoryL1）拉候选池：图侧 rand() 随机 + name 去重 + seed 洗牌。"""
    pools: dict[tuple[str, str], list[dict]] = {}
    for type_key in types:
        for cat in categories:
            rows = list(session.run(POOL_QUERIES[type_key], cat=cat, pool=pool_per_cat))
            pool = dedupe_pool(rows)
            rng.shuffle(pool)
            pools[(type_key, cat)] = pool
        log(f"[pool] {type_key}: " + ", ".join(
            f"{cat}={len(pools[(type_key, cat)])}" for cat in categories))
    return pools


def generate(
    driver,
    per_type: int = 6,
    seed: int = DEFAULT_SEED,
    pool_per_cat: int = DEFAULT_POOL_PER_CAT,
    types: Sequence[str] = QUESTION_TYPES,
    log: Callable[[str], None] = print,
) -> tuple[list[dict], dict[str, Any]]:
    with driver.session(database=NEO4J_DB) as session:
        categories = fetch_categories(session)
        if not categories:
            raise RuntimeError("图中未找到任何 categoryL1 非空的事项节点")
        pools = fetch_pools(session, categories, random.Random(seed),
                            pool_per_cat=pool_per_cat, types=types, log=log)
    return build_rows(pools, categories, per_type, types=types)


def write_csv(rows: Sequence[dict], path: str | Path) -> None:
    """13 列 testset.csv（UTF-8 BOM，列名与 data/pilot/testset.csv 一致）。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([row.get(col, "") for col in CSV_COLUMNS])


def write_ids(rows: Sequence[dict], path: str | Path) -> int:
    """去重 serviceId 每行一个（保序），返回去重后数量。"""
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        sid = _clean(row.get("service_id"))
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for sid in ids:
            fh.write(sid + "\n")
    return len(ids)


def _default_log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- CLI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_testset_multihop",
        description="从 zwdmxgj 图按模板生成 9 种题型（含 3 种反向 2 跳）的评测题集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python scripts/build_testset_multihop.py "
               "--out data/pilot/testset_multihop.csv "
               "--ids-out data/pilot/testset_multihop_service_ids.txt",
    )
    parser.add_argument("--out", required=True, metavar="CSV",
                        help="输出 testset.csv（UTF-8 BOM，12 列，test_id 前缀 MH）")
    parser.add_argument("--ids-out", metavar="TXT", default=None,
                        help="去重 serviceId 输出文件（默认 <out 去后缀>_service_ids.txt；"
                             "供 backfill_vectors.py --service-ids 回填向量）")
    parser.add_argument("--per-type", type=int, default=6, metavar="N",
                        help="每题型生成题数（默认 6，9 型共 54 题）")
    parser.add_argument("--pool-per-cat", type=int, default=DEFAULT_POOL_PER_CAT,
                        metavar="N", help="每个（题型×categoryL1）候选池大小（默认 40）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, metavar="N",
                        help="Python 侧抽样随机种子（默认 42；图侧候选池用 Neo4j rand()）")
    parser.add_argument("--types", metavar="LIST", default=None,
                        help="逗号分隔的题型子集（默认全部 9 型）。合法值："
                             + ",".join(QUESTION_TYPES)
                             + "。例：--types material,department,legal,condition,process,faq")
    args = parser.parse_args(argv)
    if args.per_type < 1:
        parser.error("--per-type 必须 >= 1")
    if args.pool_per_cat < 1:
        parser.error("--pool-per-cat 必须 >= 1")
    types = QUESTION_TYPES
    if args.types:
        picked = [t.strip() for t in args.types.split(",") if t.strip()]
        bad = [t for t in picked if t not in QUESTION_TYPES]
        if bad:
            parser.error(f"--types 含未知题型：{','.join(bad)}")
        types = tuple(picked)
    args.selected_types = types
    if not args.ids_out:
        args.ids_out = str(Path(args.out).with_suffix("")) + "_service_ids.txt"
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"# Neo4j={NEO4J_URI} db={NEO4J_DB}；per_type={args.per_type} "
          f"pool_per_cat={args.pool_per_cat} seed={args.seed}")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            rows, stats = generate(driver, per_type=args.per_type, seed=args.seed,
                                   pool_per_cat=args.pool_per_cat,
                                   types=args.selected_types, log=_default_log)
        finally:
            driver.close()
    except _NEO4J_FATAL as e:  # type: ignore[misc]
        print(f"[错误] Neo4j 执行失败（{NEO4J_URI}, db={NEO4J_DB}）：{e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2

    print("\n===== 每题型生成统计 =====")
    for type_key in args.selected_types:
        print(f"  {type_key:<18} 生成 {stats['per_type'][type_key]:>3} 题"
              f"（重抽 {stats['resampled'][type_key]} 次，"
              f"跳过超用事项 {stats['skipped_used'][type_key]} 次）")
    total = len(rows)
    short = [t for t in args.selected_types if stats["per_type"][t] < args.per_type]
    print(f"总计 {total} 题"
          + (f"（注意：{'、'.join(short)} 未达每型 {args.per_type} 题，"
             f"图数据可能偏稀，可增大 --pool-per-cat 或降低 --per-type）" if short else ""))
    if total < 50:
        print("[警告] 总题数不足 50，判分分组统计的置信度会偏低", file=sys.stderr)

    write_csv(rows, args.out)
    n_ids = write_ids(rows, args.ids_out)
    print(f"已写出：{args.out}（{total} 行）")
    print(f"已写出：{args.ids_out}（{n_ids} 个去重 serviceId）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
