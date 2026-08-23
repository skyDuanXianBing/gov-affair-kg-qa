#!/usr/bin/env python3
"""离线校验 gov_service.schema 与 OpenSPG 导入 manifest 的实体、关系和映射。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TYPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s+EntityType\s*$")
PROPERTY_RE = re.compile(r"^        ([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s+([A-Za-z][A-Za-z0-9_]*)\s*$")
RELATION_RE = re.compile(r"^        ([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s+([A-Za-z][A-Za-z0-9_]*)\s*$")
SUBPROPERTY_RE = re.compile(r"^                ([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s+")


def parse_schema(path: Path) -> dict[str, Any]:
    entities: dict[str, dict[str, Any]] = {}
    current_type: str | None = None
    section: str | None = None
    current_relation: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or raw_line.startswith("namespace "):
            continue
        type_match = TYPE_RE.match(raw_line)
        if type_match:
            current_type = type_match.group(1)
            if current_type in entities:
                raise ValueError(f"{path}:{line_number}: 重复实体 {current_type}")
            entities[current_type] = {"properties": set(), "relations": {}}
            section = None
            current_relation = None
            continue
        if current_type is None:
            raise ValueError(f"{path}:{line_number}: 无法解析: {raw_line}")
        stripped = raw_line.strip()
        if raw_line == "    properties:":
            section = "properties"
            current_relation = None
            continue
        if raw_line == "    relations:":
            section = "relations"
            current_relation = None
            continue
        if stripped.startswith("index:"):
            continue
        if section == "properties":
            match = PROPERTY_RE.match(raw_line)
            if not match:
                raise ValueError(f"{path}:{line_number}: 属性语法错误: {raw_line}")
            entities[current_type]["properties"].add(match.group(1))
            continue
        if section == "relations":
            subproperty_match = SUBPROPERTY_RE.match(raw_line)
            if subproperty_match:
                if not current_relation:
                    raise ValueError(f"{path}:{line_number}: 关系子属性没有所属关系")
                entities[current_type]["relations"][current_relation]["properties"].add(
                    subproperty_match.group(1)
                )
                continue
            relation_match = RELATION_RE.match(raw_line)
            if not relation_match:
                if raw_line == "            properties:":
                    continue
                raise ValueError(f"{path}:{line_number}: 关系语法错误: {raw_line}")
            current_relation = relation_match.group(1)
            entities[current_type]["relations"][current_relation] = {
                "target": relation_match.group(2),
                "properties": set(),
            }
            continue
        raise ValueError(f"{path}:{line_number}: 未知段落: {raw_line}")
    return entities


def validate_manifest(manifest_path: Path, entities: dict[str, Any]) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for job in manifest["jobs"]:
        target = job["schema_target"]
        mapping_targets = {
            value for values in job["mapping"].values() for value in values
        }
        if target["kind"] == "entity":
            type_name = target["type"]
            if type_name not in entities:
                raise ValueError(f"{manifest_path}: {job['key']} 实体不存在: {type_name}")
            valid = entities[type_name]["properties"] | {"id"}
        else:
            source = target["source_type"].rsplit(".", 1)[-1]
            target_type = target["target_type"].rsplit(".", 1)[-1]
            relation = target["relation"]
            if source not in entities or relation not in entities[source]["relations"]:
                raise ValueError(f"{manifest_path}: {job['key']} 关系不存在: {source}.{relation}")
            relation_schema = entities[source]["relations"][relation]
            if relation_schema["target"] != target_type:
                raise ValueError(
                    f"{manifest_path}: {job['key']} 关系终点错误: "
                    f"schema={relation_schema['target']} manifest={target_type}"
                )
            valid = relation_schema["properties"] | {"start_id", "end_id"}
        missing = mapping_targets - valid
        if missing:
            raise ValueError(f"{manifest_path}: {job['key']} 映射目标不存在: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=ROOT / "schema" / "gov_service.schema")
    parser.add_argument(
        "--manifest", type=Path, action="append",
        default=[
            ROOT / "schema" / "openspg_import_manifest.json",
            ROOT / "schema" / "openspg_personal_import_manifest.json",
        ],
    )
    args = parser.parse_args()
    entities = parse_schema(args.schema)
    for manifest in args.manifest:
        validate_manifest(manifest, entities)
    relation_count = sum(len(item["relations"]) for item in entities.values())
    print(
        f"VALID schema={args.schema} entities={len(entities)} relations={relation_count} "
        f"manifests={len(args.manifest)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
