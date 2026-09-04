#!/usr/bin/env python3
"""校验统一 namespace 的领域、分类、知识模型和事项模型关系。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "build" / "openspg-model-metadata"
DATASETS = ("pilot", "personal")


def rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def require_unique(path: Path, key: str) -> tuple[set[str], int]:
    values: set[str] = set()
    count = 0
    for row in rows(path):
        count += 1
        value = (row.get(key) or "").strip()
        if not value:
            raise ValueError(f"{path}: {key} 为空，行号={count + 1}")
        if value in values:
            raise ValueError(f"{path}: {key} 重复: {value}")
        values.add(value)
    return values, count


def validate_relation(path: Path, start_ids: set[str], end_ids: set[str]) -> int:
    count = 0
    for row in rows(path):
        count += 1
        start_id = (row.get("start_id") or "").strip()
        end_id = (row.get("end_id") or "").strip()
        if start_id not in start_ids:
            raise ValueError(f"{path}: 起点不存在: {start_id}")
        if end_id not in end_ids:
            raise ValueError(f"{path}: 终点不存在: {end_id}")
    return count


def validate_dataset(root: Path, dataset: str, domains: set[str], categories: set[str], models: set[str]) -> dict[str, int]:
    services_path = root / dataset / "services.csv"
    service_count = 0
    with services_path.open("r", encoding="utf-8-sig", newline="") as services_handle:
        services_reader = csv.DictReader(services_handle)
        required = {"service_id", "domain_id", "category_id", "model_id"}
        missing = required - set(services_reader.fieldnames or [])
        if missing:
            raise ValueError(f"{services_path}: 缺少字段 {sorted(missing)}")
        relation_handles = {
            "domain": (root / dataset / "service_belongs_to_domain.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ),
            "category": (root / dataset / "service_classified_as.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ),
            "model": (root / dataset / "service_uses_model.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ),
        }
        try:
            relation_readers = {key: csv.DictReader(handle) for key, handle in relation_handles.items()}
            for service in services_reader:
                service_count += 1
                service_id = service["service_id"].strip()
                domain_id = service["domain_id"].strip()
                category_id = service["category_id"].strip()
                model_id = service["model_id"].strip()
                if domain_id not in domains or category_id not in categories or model_id not in models:
                    raise ValueError(f"{services_path}: 模型引用不存在: {service_id}")
                expected = {
                    "domain": (service_id, domain_id),
                    "category": (service_id, category_id),
                    "model": (service_id, model_id),
                }
                for key, reader in relation_readers.items():
                    relation = next(reader, None)
                    if relation is None:
                        raise ValueError(f"{dataset}: {key} 关系少于 services.csv")
                    actual = (relation["start_id"].strip(), relation["end_id"].strip())
                    if actual != expected[key]:
                        raise ValueError(
                            f"{dataset}: {key} 关系与事项不一致，expected={expected[key]} actual={actual}"
                        )
            for key, reader in relation_readers.items():
                if next(reader, None) is not None:
                    raise ValueError(f"{dataset}: {key} 关系多于 services.csv")
        finally:
            for handle in relation_handles.values():
                handle.close()
    return {"services": service_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    common = root / "common"

    domains, domain_count = require_unique(common / "service_domains.csv", "domain_id")
    schemes, scheme_count = require_unique(common / "category_schemes.csv", "scheme_id")
    categories, category_count = require_unique(common / "service_categories.csv", "category_id")
    models, model_count = require_unique(common / "knowledge_models.csv", "model_id")

    category_rows = list(rows(common / "service_categories.csv"))
    model_rows = list(rows(common / "knowledge_models.csv"))
    for row in category_rows:
        if row["domain_id"] not in domains or row["scheme_id"] not in schemes:
            raise ValueError(f"分类引用不存在: {row['category_id']}")
        parent = row["parent_category_id"].strip()
        if parent and parent not in categories:
            raise ValueError(f"父分类不存在: {row['category_id']} -> {parent}")
    for row in model_rows:
        if row["domain_id"] not in domains:
            raise ValueError(f"模型领域不存在: {row['model_id']}")
        category = row["category_id"].strip()
        if category and category not in categories:
            raise ValueError(f"模型分类不存在: {row['model_id']}")
        for field in ("enabled_entity_types", "enabled_relation_types", "retrieval_filter", "validation_profile"):
            json.loads(row[field])

    relation_counts = {
        "category_parent": validate_relation(common / "category_parent.csv", categories, categories),
        "category_belongs_to_scheme": validate_relation(
            common / "category_belongs_to_scheme.csv", categories, schemes
        ),
        "category_belongs_to_domain": validate_relation(
            common / "category_belongs_to_domain.csv", categories, domains
        ),
        "model_applies_to_category": validate_relation(
            common / "model_applies_to_category.csv", models, categories
        ),
        "model_belongs_to_domain": validate_relation(
            common / "model_belongs_to_domain.csv", models, domains
        ),
        "model_extends_model": validate_relation(
            common / "model_extends_model.csv", models, models
        ),
    }
    dataset_counts = {
        dataset: validate_dataset(root, dataset, domains, categories, models)
        for dataset in DATASETS
    }
    summary = {
        "valid": True,
        "domains": domain_count,
        "schemes": scheme_count,
        "categories": category_count,
        "models": model_count,
        "relations": relation_counts,
        "datasets": dataset_counts,
    }
    output = root / "validation_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
