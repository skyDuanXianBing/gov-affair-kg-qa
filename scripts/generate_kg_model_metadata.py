#!/usr/bin/env python3
"""生成统一 namespace 下的领域、分类、知识模型元数据及分类关系 CSV。

输出全部写入 build/，不会修改原始 dataset CSV。服务和 Chunk 的增强 CSV
会在保留旧 categoryL1/categoryL2 字段的同时增加 domainId/categoryId/modelId。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "build" / "openspg-model-metadata"
DATASET_CONFIG = {
    "pilot": {
        "domain_id": "domain:corporate",
        "domain_name": "法人服务",
        "scheme_id": "scheme:corporate_subject",
        "scheme_name": "法人服务主题分类体系",
        "services": ROOT / "build" / "openspg-prepared" / "pilot" / "services.csv",
        "documents": ROOT / "dataset" / "pilot" / "documents_chunks.csv",
    },
    "personal": {
        "domain_id": "domain:personal",
        "domain_name": "个人事务",
        "scheme_id": "scheme:personal_service_type",
        "scheme_name": "个人事务事项类型分类体系",
        "services": ROOT / "build" / "openspg-prepared" / "personal" / "services.csv",
        "documents": ROOT / "dataset" / "personal" / "documents.csv",
    },
}
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
    "category_parent": ["start_id", "end_id"],
    "category_belongs_to_scheme": ["start_id", "end_id"],
    "category_belongs_to_domain": ["start_id", "end_id"],
    "model_applies_to_category": ["start_id", "end_id"],
    "model_belongs_to_domain": ["start_id", "end_id"],
    "model_extends_model": ["start_id", "end_id"],
}
SERVICE_RELATION_FIELDS = {
    "service_belongs_to_domain": ["start_id", "end_id"],
    "service_classified_as": ["start_id", "end_id"],
    "service_uses_model": ["start_id", "end_id"],
}


def clean(value: object) -> str:
    return str(value or "").strip()


def safe_component(value: str) -> str:
    value = re.sub(r"\s+", "_", clean(value))
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return value or hashlib.sha1(clean(value).encode("utf-8")).hexdigest()[:16]


def category_id(domain_key: str, name: str) -> str:
    return f"category:{domain_key}:{safe_component(name)}"


def model_id(domain_key: str, name: str) -> str:
    return f"model:{domain_key}:{safe_component(name)}"


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


def collect_categories() -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for dataset, config in DATASET_CONFIG.items():
        categories: set[str] = set()
        with config["services"].open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                category = clean(row.get("category_l2")) or "未分类"
                categories.add(category)
        domain_key = "corporate" if dataset == "pilot" else "personal"
        identifiers = [category_id(domain_key, name) for name in categories]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"{dataset} 分类名称规范化后产生 ID 冲突")
        result[dataset] = [{"name": name} for name in sorted(categories)]
    return result


BASE_ENTITY_TYPES = [
    "GovernmentService", "Department", "Material", "ServiceCondition", "ProcessStep",
    "ServiceResult", "LegalBasis", "FAQ", "ServiceChannel", "Fee", "Chunk",
]
BASE_RELATION_TYPES = [
    "handledBy", "collaboratesWith", "requiresMaterial", "hasCondition", "hasProcessStep",
    "nextStep", "producesResult", "basedOn", "hasFaq", "hasChannel", "hasFee",
    "belongsToDomain", "classifiedAs", "usesModel",
]


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_common_rows(category_groups: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    domains: list[dict[str, str]] = []
    schemes: list[dict[str, str]] = []
    categories: list[dict[str, str]] = []
    models: list[dict[str, str]] = []
    category_parent: list[dict[str, str]] = []
    category_scheme: list[dict[str, str]] = []
    category_domain: list[dict[str, str]] = []
    model_category: list[dict[str, str]] = []
    model_domain: list[dict[str, str]] = []
    model_extends: list[dict[str, str]] = []

    base_models: list[tuple[str, str, str]] = []
    for domain_key, config in (("corporate", DATASET_CONFIG["pilot"]), ("personal", DATASET_CONFIG["personal"])):
        domain_id = config["domain_id"]
        scheme_id = config["scheme_id"]
        root_category_id = category_id(domain_key, "根分类")
        base_model = model_id(domain_key, "base")
        base_models.append((domain_key, domain_id, base_model))
        domains.append({
            "domain_id": domain_id, "name": config["domain_name"],
            "description": f"{config['domain_name']}政务服务领域统一知识模型",
            "status": "ACTIVE", "version": "v1",
        })
        schemes.append({
            "scheme_id": scheme_id, "name": config["scheme_name"], "domain_id": domain_id,
            "description": f"{config['domain_name']}的分类体系", "version": "v1",
        })
        categories.append({
            "category_id": root_category_id, "name": config["domain_name"],
            "category_level": "1", "parent_category_id": "", "scheme_id": scheme_id,
            "domain_id": domain_id, "description": f"{config['domain_name']}根分类",
            "status": "ACTIVE", "version": "v1",
        })
        models.append({
            "model_id": base_model, "name": f"{config['domain_name']}基础知识模型",
            "model_type": "DOMAIN_BASE", "domain_id": domain_id, "category_id": "",
            "version": "v1", "description": f"{config['domain_name']}共享基础模型",
            "schema_version": "gov-service-v2",
            "enabled_entity_types": json_text(BASE_ENTITY_TYPES),
            "enabled_relation_types": json_text(BASE_RELATION_TYPES),
            "retrieval_filter": json_text({"domain_id": domain_id}),
            "validation_profile": json_text({"required": ["serviceId", "domainId", "categoryId"]}),
            "status": "ACTIVE",
        })
        model_domain.append({"start_id": base_model, "end_id": domain_id})

        for item in category_groups["pilot" if domain_key == "corporate" else "personal"]:
            name = item["name"]
            child_id = category_id(domain_key, name)
            child_model_id = model_id(domain_key, name)
            categories.append({
                "category_id": child_id, "name": name, "category_level": "2",
                "parent_category_id": root_category_id, "scheme_id": scheme_id,
                "domain_id": domain_id, "description": f"{config['domain_name']}：{name}",
                "status": "ACTIVE", "version": "v1",
            })
            models.append({
                "model_id": child_model_id, "name": f"{config['domain_name']}-{name}知识模型",
                "model_type": "CATEGORY_PROFILE", "domain_id": domain_id,
                "category_id": child_id, "version": "v1",
                "description": f"面向{config['domain_name']}“{name}”分类的知识模型 Profile",
                "schema_version": "gov-service-v2",
                "enabled_entity_types": json_text(BASE_ENTITY_TYPES),
                "enabled_relation_types": json_text(BASE_RELATION_TYPES),
                "retrieval_filter": json_text({"domain_id": domain_id, "category_id": child_id}),
                "validation_profile": json_text({"required": ["serviceId", "domainId", "categoryId"]}),
                "status": "ACTIVE",
            })
            category_parent.append({"start_id": child_id, "end_id": root_category_id})
            category_scheme.append({"start_id": child_id, "end_id": scheme_id})
            category_domain.append({"start_id": child_id, "end_id": domain_id})
            model_category.append({"start_id": child_model_id, "end_id": child_id})
            model_domain.append({"start_id": child_model_id, "end_id": domain_id})
            model_extends.append({"start_id": child_model_id, "end_id": base_model})
        category_scheme.append({"start_id": root_category_id, "end_id": scheme_id})
        category_domain.append({"start_id": root_category_id, "end_id": domain_id})

    return {
        "service_domains": domains,
        "category_schemes": schemes,
        "service_categories": categories,
        "knowledge_models": models,
        "category_parent": category_parent,
        "category_belongs_to_scheme": category_scheme,
        "category_belongs_to_domain": category_domain,
        "model_applies_to_category": model_category,
        "model_belongs_to_domain": model_domain,
        "model_extends_model": model_extends,
    }


def enrich_services(dataset: str, output_root: Path) -> Path:
    config = DATASET_CONFIG[dataset]
    source = config["services"]
    output = output_root / dataset / "services.csv"
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        for field in ("domain_id", "category_id", "model_id"):
            if field not in fieldnames:
                fieldnames.append(field)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                category = clean(row.get("category_l2")) or "未分类"
                domain_key = "corporate" if dataset == "pilot" else "personal"
                row["domain_id"] = DATASET_CONFIG[dataset]["domain_id"]
                row["category_id"] = category_id(domain_key, category)
                row["model_id"] = model_id(domain_key, category)
                writer.writerow(row)
    return output


def enrich_chunk_csv(source: Path, output: Path, dataset: str) -> Path:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        for field in ("domain_id", "category_id", "model_id"):
            if field not in fieldnames:
                fieldnames.append(field)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                category = clean(row.get("category_l2")) or "未分类"
                domain_key = "corporate" if dataset == "pilot" else "personal"
                row["domain_id"] = DATASET_CONFIG[dataset]["domain_id"]
                row["category_id"] = category_id(domain_key, category)
                row["model_id"] = model_id(domain_key, category)
                writer.writerow(row)
    return output


def write_dataset_relations(dataset: str, output_root: Path) -> None:
    """单次扫描 services.csv，同时流式写出三类事项模型关系。"""
    config = DATASET_CONFIG[dataset]
    domain_key = "corporate" if dataset == "pilot" else "personal"
    service_path = config["services"]
    relation_root = output_root / dataset
    relation_root.mkdir(parents=True, exist_ok=True)
    handles = {}
    writers = {}
    try:
        for key, fieldnames in SERVICE_RELATION_FIELDS.items():
            handle = (relation_root / f"{key}.csv").open("w", encoding="utf-8", newline="")
            handles[key] = handle
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writers[key] = writer
        with service_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                service_id = clean(row.get("service_id"))
                category = clean(row.get("category_l2")) or "未分类"
                if not service_id:
                    continue
                writers["service_belongs_to_domain"].writerow(
                    {"start_id": service_id, "end_id": config["domain_id"]}
                )
                writers["service_classified_as"].writerow(
                    {"start_id": service_id, "end_id": category_id(domain_key, category)}
                )
                writers["service_uses_model"].writerow(
                    {"start_id": service_id, "end_id": model_id(domain_key, category)}
                )
    finally:
        for handle in handles.values():
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-chunk-enrichment", action="store_true")
    args = parser.parse_args()
    output_root = args.output_root.expanduser().resolve()
    category_groups = collect_categories()
    common_rows = build_common_rows(category_groups)
    for key, rows in common_rows.items():
        write_csv(output_root / "common" / f"{key}.csv", COMMON_FIELDS[key], rows)
    for dataset in DATASET_CONFIG:
        enrich_services(dataset, output_root)
        write_dataset_relations(dataset, output_root)
        if not args.skip_chunk_enrichment and dataset == "pilot":
            enrich_chunk_csv(
                DATASET_CONFIG[dataset]["documents"],
                output_root / dataset / "documents_chunks.csv",
                dataset,
            )
    print(f"生成完成：{output_root}")
    print(f"法人分类：{len(category_groups['pilot'])} 个；个人分类：{len(category_groups['personal'])} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
