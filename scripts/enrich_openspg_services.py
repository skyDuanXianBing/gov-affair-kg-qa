#!/usr/bin/env python3
"""为两套政务事项 CSV 增加可审计的结构化字段。

个人事务的附加字段来自 documents.csv 的 extras_json；pilot 数据集保留同一套列，
但对没有统一键名的扩展 JSON 不做猜测，字段留空并保留原文在 documents.csv 中。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENRICHED_FIELDS = [
    "dataset_scope",
    "service_object",
    "exercise_level",
    "service_status",
    "online_depth",
    "online_available",
    "promise_time_limit",
    "legal_time_limit",
    "official_list_count",
    "detail_return_code",
    "source_record_sha256",
    "official_json_sha256",
]


def clean(value: Any) -> str:
    """把附加字段安全地转换为单行 CSV 文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def atomic_replace(output_path: Path, writer_callback) -> None:
    """使用临时文件写出，完成后原子替换目标。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        writer_callback(temp_path)
        temp_path.replace(output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def enrich_personal(services_path: Path, documents_path: Path, output_path: Path) -> dict[str, int]:
    """按 services/documents 同一规范化顺序合并个人事务字段。"""
    stats = {"rows": 0, "invalid_json": 0, "id_mismatch": 0}

    def write(temp_path: Path) -> None:
        with (
            services_path.open("r", encoding="utf-8-sig", newline="") as service_fh,
            documents_path.open("r", encoding="utf-8-sig", newline="") as document_fh,
            temp_path.open("w", encoding="utf-8", newline="") as output_fh,
        ):
            services = csv.DictReader(service_fh)
            documents = csv.DictReader(document_fh)
            service_fields = services.fieldnames or []
            required_services = {"service_id"} - set(service_fields)
            required_documents = {"service_id", "extras_json"} - set(documents.fieldnames or [])
            if required_services or required_documents:
                raise ValueError(
                    f"输入列缺失 services={sorted(required_services)} documents={sorted(required_documents)}"
                )
            fieldnames = service_fields + [field for field in ENRICHED_FIELDS if field not in service_fields]
            writer = csv.DictWriter(output_fh, fieldnames=fieldnames, lineterminator="\n", extrasaction="raise")
            writer.writeheader()
            for service, document in zip(services, documents):
                stats["rows"] += 1
                if service.get("service_id") != document.get("service_id"):
                    stats["id_mismatch"] += 1
                    raise ValueError(
                        f"services/documents 顺序或 service_id 不一致："
                        f"{service.get('service_id')} != {document.get('service_id')}"
                    )
                try:
                    extras = json.loads(document.get("extras_json") or "{}")
                except json.JSONDecodeError as exc:
                    stats["invalid_json"] += 1
                    raise ValueError(f"documents extras_json 第 {stats['rows']} 行不是合法 JSON") from exc
                if not isinstance(extras, dict):
                    raise ValueError(f"documents extras_json 第 {stats['rows']} 行不是对象")
                row = dict(service)
                row.update({field: clean(extras.get(field)) for field in ENRICHED_FIELDS})
                row["dataset_scope"] = "personal"
                writer.writerow(row)

    atomic_replace(output_path, write)
    return stats


def enrich_pilot(services_path: Path, output_path: Path) -> dict[str, int]:
    """为 pilot 保持统一列结构；未知扩展字段不做启发式猜测。"""
    stats = {"rows": 0}

    def write(temp_path: Path) -> None:
        with services_path.open("r", encoding="utf-8-sig", newline="") as input_fh, temp_path.open(
            "w", encoding="utf-8", newline=""
        ) as output_fh:
            reader = csv.DictReader(input_fh)
            service_fields = reader.fieldnames or []
            if "service_id" not in service_fields:
                raise ValueError("services.csv 缺少 service_id")
            fieldnames = service_fields + [field for field in ENRICHED_FIELDS if field not in service_fields]
            writer = csv.DictWriter(output_fh, fieldnames=fieldnames, lineterminator="\n", extrasaction="raise")
            writer.writeheader()
            for row in reader:
                stats["rows"] += 1
                output = dict(row)
                output.update({field: "" for field in ENRICHED_FIELDS})
                output["dataset_scope"] = "pilot"
                writer.writerow(output)

    atomic_replace(output_path, write)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 OpenSPG 两套数据集的扩展 services.csv")
    parser.add_argument("--dataset", choices=["personal", "pilot"], required=True)
    parser.add_argument("--input-root", type=Path, default=ROOT / "dataset")
    parser.add_argument("--output-root", type=Path, default=ROOT / "build" / "openspg-prepared")
    args = parser.parse_args()
    input_root = args.input_root.expanduser().resolve() / args.dataset
    output_path = args.output_root.expanduser().resolve() / args.dataset / "services.csv"
    if args.dataset == "personal":
        stats = enrich_personal(input_root / "services.csv", input_root / "documents.csv", output_path)
    else:
        stats = enrich_pilot(input_root / "services.csv", output_path)
    print(json.dumps({"dataset": args.dataset, "output": str(output_path), **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
