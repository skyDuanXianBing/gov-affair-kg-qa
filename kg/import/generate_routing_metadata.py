#!/usr/bin/env python3
"""生成 ZwdmxGJ 路由层元数据 CSV（阶段一 pilot：法人服务）。

参照 scripts/generate_kg_model_metadata.py 的思路，适配 schemas/ZwdmxGJ-v0.3：
- 领域 domain:corporate（法人服务），分类体系 scheme:corporate_subject
- 二级分类取 data/pilot/services.csv 的 category_l2 distinct（空值归"未分类"）
- 每分类生成 CATEGORY_PROFILE 知识模型，均 extends 基础模型
- 事项三关系（belongsToDomain/classifiedAs/usesModel）从 services.csv 流式生成
- 输出 kg/import/routing_metadata/；不改写 data/ 任何文件

services 节点的 domainId/categoryId/modelId 属性增补由 run_import.py 在
分片时流式追加列（见 SERVICES_ENRICH_COLUMNS），不落盘全量副本。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "kg" / "import" / "routing_metadata"
SERVICES_CSV = ROOT / "data" / "pilot" / "services.csv"

DOMAIN_ID = "domain:corporate"
DOMAIN_NAME = "法人服务"
SCHEME_ID = "scheme:corporate_subject"
SCHEME_NAME = "法人服务主题分类体系"
SCHEMA_VERSION = "ZwdmxGJ-v0.3"
DOMAIN_KEY = "corporate"

BASE_ENTITY_TYPES = [
    "ServiceDomain", "CategoryScheme", "ServiceCategory", "KnowledgeModel",
    "GovernmentService", "Department", "Material", "ServiceCondition", "ProcessStep",
    "ServiceResult", "LegalBasis", "LegalCitation", "FAQ", "ServiceChannel", "Fee",
    "Chunk", "Proposition",
]
BASE_RELATION_TYPES = [
    "belongsToDomain", "belongsToScheme", "parentCategory", "appliesToCategory",
    "extendsModel", "classifiedAs", "usesModel", "handledBy", "collaboratesWith",
    "requiresMaterial", "hasCondition", "hasProcessStep", "nextStep", "producesResult",
    "citesLegal", "partOf", "hasFaq", "hasChannel", "hasFee", "belongsTo", "hasChunk",
]

COMMON_FIELDS = {
    "service_domains": ["domain_id", "name", "description", "status", "version"],
    "category_schemes": ["scheme_id", "name", "domain_id", "description", "version"],
    "service_categories": [
        "category_id", "name", "category_level", "parent_category_id", "scheme_id",
        "domain_id", "description", "status", "version",
    ],
    "knowledge_models": [
        "model_id", "name", "model_type", "domain_id", "category_id", "version",
        "description", "schema_version", "enabled_entity_types", "enabled_relation_types",
        "retrieval_filter", "validation_profile", "status",
    ],
}
RELATION_FIELDS = {
    key: ["start_id", "end_id"]
    for key in (
        "category_parent", "category_belongs_to_scheme", "category_belongs_to_domain",
        "model_applies_to_category", "model_belongs_to_domain", "model_extends_model",
        "service_belongs_to_domain", "service_classified_as", "service_uses_model",
    )
}

# services 分片时追加的列（映射到 GovernmentService.domainId/categoryId/modelId）
SERVICES_ENRICH_COLUMNS = ["domain_id", "category_id", "model_id"]


def clean(value: object) -> str:
    return str(value or "").strip()


def safe_component(value: str) -> str:
    value = re.sub(r"\s+", "_", clean(value))
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return value or hashlib.sha1(clean(value).encode("utf-8")).hexdigest()[:16]


def category_id(name: str) -> str:
    return f"category:{DOMAIN_KEY}:{safe_component(name)}"


def model_id(name: str) -> str:
    return f"model:{DOMAIN_KEY}:{safe_component(name)}"


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fieldnames})
            count += 1
    return count


def collect_categories() -> list[str]:
    categories: set[str] = set()
    with SERVICES_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            categories.add(clean(row.get("category_l2")) or "未分类")
    identifiers = [category_id(name) for name in categories]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("法人分类名称规范化后产生 ID 冲突")
    return sorted(categories)


def build_rows(categories: list[str]) -> dict[str, list[dict[str, str]]]:
    domains: list[dict[str, str]] = [{
        "domain_id": DOMAIN_ID, "name": DOMAIN_NAME,
        "description": f"{DOMAIN_NAME}政务服务领域统一知识模型",
        "status": "ACTIVE", "version": "v1",
    }]
    schemes: list[dict[str, str]] = [{
        "scheme_id": SCHEME_ID, "name": SCHEME_NAME, "domain_id": DOMAIN_ID,
        "description": f"{DOMAIN_NAME}的分类体系", "version": "v1",
    }]
    root_category_id = category_id("根分类")
    cat_rows: list[dict[str, str]] = [{
        "category_id": root_category_id, "name": DOMAIN_NAME,
        "category_level": "1", "parent_category_id": "", "scheme_id": SCHEME_ID,
        "domain_id": DOMAIN_ID, "description": f"{DOMAIN_NAME}根分类",
        "status": "ACTIVE", "version": "v1",
    }]
    base_model = model_id("base")
    model_rows: list[dict[str, str]] = [{
        "model_id": base_model, "name": f"{DOMAIN_NAME}基础知识模型",
        "model_type": "DOMAIN_BASE", "domain_id": DOMAIN_ID, "category_id": "",
        "version": "v1", "description": f"{DOMAIN_NAME}共享基础模型",
        "schema_version": SCHEMA_VERSION,
        "enabled_entity_types": json_text(BASE_ENTITY_TYPES),
        "enabled_relation_types": json_text(BASE_RELATION_TYPES),
        "retrieval_filter": json_text({"domain_id": DOMAIN_ID}),
        "validation_profile": json_text({"required": ["serviceId", "domainId", "categoryId"]}),
        "status": "ACTIVE",
    }]
    category_parent: list[dict[str, str]] = []
    category_scheme: list[dict[str, str]] = []
    category_domain: list[dict[str, str]] = [
        {"start_id": root_category_id, "end_id": DOMAIN_ID},
    ]
    model_category: list[dict[str, str]] = []
    model_domain: list[dict[str, str]] = [{"start_id": base_model, "end_id": DOMAIN_ID}]
    model_extends: list[dict[str, str]] = []

    category_scheme.append({"start_id": root_category_id, "end_id": SCHEME_ID})
    for name in categories:
        child_id = category_id(name)
        child_model_id = model_id(name)
        cat_rows.append({
            "category_id": child_id, "name": name, "category_level": "2",
            "parent_category_id": root_category_id, "scheme_id": SCHEME_ID,
            "domain_id": DOMAIN_ID, "description": f"{DOMAIN_NAME}：{name}",
            "status": "ACTIVE", "version": "v1",
        })
        model_rows.append({
            "model_id": child_model_id, "name": f"{DOMAIN_NAME}-{name}知识模型",
            "model_type": "CATEGORY_PROFILE", "domain_id": DOMAIN_ID,
            "category_id": child_id, "version": "v1",
            "description": f"面向{DOMAIN_NAME}“{name}”分类的知识模型 Profile",
            "schema_version": SCHEMA_VERSION,
            "enabled_entity_types": json_text(BASE_ENTITY_TYPES),
            "enabled_relation_types": json_text(BASE_RELATION_TYPES),
            "retrieval_filter": json_text({"domain_id": DOMAIN_ID, "category_id": child_id}),
            "validation_profile": json_text({"required": ["serviceId", "domainId", "categoryId"]}),
            "status": "ACTIVE",
        })
        category_parent.append({"start_id": child_id, "end_id": root_category_id})
        category_scheme.append({"start_id": child_id, "end_id": SCHEME_ID})
        category_domain.append({"start_id": child_id, "end_id": DOMAIN_ID})
        model_category.append({"start_id": child_model_id, "end_id": child_id})
        model_domain.append({"start_id": child_model_id, "end_id": DOMAIN_ID})
        model_extends.append({"start_id": child_model_id, "end_id": base_model})

    return {
        "service_domains": domains,
        "category_schemes": schemes,
        "service_categories": cat_rows,
        "knowledge_models": model_rows,
        "category_parent": category_parent,
        "category_belongs_to_scheme": category_scheme,
        "category_belongs_to_domain": category_domain,
        "model_applies_to_category": model_category,
        "model_belongs_to_domain": model_domain,
        "model_extends_model": model_extends,
    }


def write_service_relations() -> dict[str, int]:
    counts = {key: 0 for key in ("service_belongs_to_domain", "service_classified_as", "service_uses_model")}
    handles = {}
    writers = {}
    try:
        for key, fieldnames in RELATION_FIELDS.items():
            if not key.startswith("service_"):
                continue
            path = OUTPUT_DIR / f"{key}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", encoding="utf-8", newline="")
            handles[key] = handle
            writers[key] = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writers[key].writeheader()
        with SERVICES_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                service_id = clean(row.get("service_id"))
                if not service_id:
                    continue
                category = clean(row.get("category_l2")) or "未分类"
                writers["service_belongs_to_domain"].writerow(
                    {"start_id": service_id, "end_id": DOMAIN_ID}
                )
                writers["service_classified_as"].writerow(
                    {"start_id": service_id, "end_id": category_id(category)}
                )
                writers["service_uses_model"].writerow(
                    {"start_id": service_id, "end_id": model_id(category)}
                )
                for key in counts:
                    counts[key] += 1
    finally:
        for handle in handles.values():
            handle.close()
    return counts


def enrich_service_row(row: dict[str, str]) -> dict[str, str]:
    """run_import 分片 services 时逐行调用，追加路由三列。"""
    category = clean(row.get("category_l2")) or "未分类"
    row["domain_id"] = DOMAIN_ID
    row["category_id"] = category_id(category)
    row["model_id"] = model_id(category)
    return row


def main() -> int:
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir.expanduser().resolve()

    categories = collect_categories()
    rows = build_rows(categories)
    summary: dict[str, int] = {}
    for key, fieldnames in COMMON_FIELDS.items():
        summary[key] = write_csv(OUTPUT_DIR / f"{key}.csv", fieldnames, rows[key])
    for key, fieldnames in RELATION_FIELDS.items():
        if key.startswith("service_"):
            continue
        summary[key] = write_csv(OUTPUT_DIR / f"{key}.csv", fieldnames, rows[key])
    summary.update(write_service_relations())
    stats_path = OUTPUT_DIR / "stats.json"
    stats_path.write_text(
        json.dumps(
            {"categories": len(categories), "files": summary, "schema_version": SCHEMA_VERSION},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"生成完成：{OUTPUT_DIR}")
    print(f"法人 L2 分类：{len(categories)} 个；stats: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
