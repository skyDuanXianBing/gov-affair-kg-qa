#!/usr/bin/env python3
"""Stream-validate normalized OpenSPG entity, relation, document and Chunk CSVs."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

ENTITY_FILES = {
    "services.csv": ("service", "service_id"),
    "departments.csv": ("department", "department_id"),
    "materials.csv": ("material", "material_id"),
    "conditions.csv": ("condition", "condition_id"),
    "process_steps.csv": ("process_step", "process_step_id"),
    "results.csv": ("result", "result_id"),
    "legal_bases.csv": ("legal_basis", "legal_basis_id"),
    "faqs.csv": ("faq", "faq_id"),
    "service_channels.csv": ("channel", "channel_id"),
    "fees.csv": ("fee", "fee_id"),
}

RELATION_FILES = {
    "service_handled_by.csv": [("service_id", "service"), ("department_id", "department")],
    "service_collaborates_with.csv": [("service_id", "service"), ("department_id", "department")],
    "service_requires_material.csv": [("service_id", "service"), ("material_id", "material")],
    "service_has_condition.csv": [("service_id", "service"), ("condition_id", "condition")],
    "service_has_process_step.csv": [("service_id", "service"), ("process_step_id", "process_step")],
    "process_step_next.csv": [
        ("service_id", "service"),
        ("from_process_step_id", "process_step"),
        ("to_process_step_id", "process_step"),
    ],
    "service_produces_result.csv": [("service_id", "service"), ("result_id", "result")],
    "service_based_on.csv": [("service_id", "service"), ("legal_basis_id", "legal_basis")],
    "service_has_faq.csv": [("service_id", "service"), ("faq_id", "faq")],
    "service_has_channel.csv": [("service_id", "service"), ("channel_id", "channel")],
    "service_has_fee.csv": [("service_id", "service"), ("fee_id", "fee")],
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("root", type=Path, help="Normalized CSV directory")
    result.add_argument("--database", type=Path, default=None, help="Temporary SQLite path")
    result.add_argument("--max-chunk-chars", type=int, default=2000)
    result.add_argument("--progress-every", type=int, default=1_000_000)
    result.add_argument("--min-free-gib", type=float, default=4.0)
    return result


def ensure_columns(reader: csv.DictReader, filename: str, columns: list[str], errors: list[str]) -> bool:
    fields = set(reader.fieldnames or [])
    missing = [column for column in columns if column not in fields]
    if missing:
        errors.append(f"{filename}: missing columns {missing}")
        return False
    return True


def configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-131072")


def free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def main() -> int:
    args = parser().parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    database = args.database or root / "manifests" / ".validation.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    configure(connection)
    summary: dict[str, Any] = {
        "root": str(root),
        "entities": {},
        "relations": {},
        "documents": {},
        "chunks": {},
        "errors": [],
    }
    started = time.monotonic()

    def progress(label: str, rows: int) -> None:
        if args.progress_every and rows % args.progress_every == 0:
            elapsed = max(time.monotonic() - started, 0.001)
            available = free_gib(root)
            print(f"progress {label} rows={rows} rate={rows / elapsed:.1f}/s free={available:.1f}GiB", flush=True)
            if available < args.min_free_gib:
                raise RuntimeError(
                    f"free disk space {available:.2f} GiB is below --min-free-gib={args.min_free_gib:.2f}"
                )

    try:
        for filename, (kind, key) in ENTITY_FILES.items():
            path = root / filename
            rows = duplicates = empty = 0
            connection.execute(f"CREATE TABLE {kind}(id TEXT PRIMARY KEY) WITHOUT ROWID")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not ensure_columns(reader, filename, [key], summary["errors"]):
                    continue
                batch: list[tuple[str]] = []
                for row in reader:
                    rows += 1
                    value = (row.get(key) or "").strip()
                    if not value:
                        empty += 1
                        continue
                    batch.append((value,))
                    if len(batch) >= 10_000:
                        before = connection.total_changes
                        connection.executemany(f"INSERT OR IGNORE INTO {kind}(id) VALUES (?)", batch)
                        duplicates += len(batch) - (connection.total_changes - before)
                        batch.clear()
                    progress(filename, rows)
                if batch:
                    before = connection.total_changes
                    connection.executemany(f"INSERT OR IGNORE INTO {kind}(id) VALUES (?)", batch)
                    duplicates += len(batch) - (connection.total_changes - before)
            connection.commit()
            summary["entities"][kind] = {
                "file": filename,
                "rows": rows,
                "duplicate_ids": duplicates,
                "empty_ids": empty,
            }
            print("entity", filename, summary["entities"][kind], flush=True)

        lookup_statements = {
            kind: connection.cursor() for kind, _ in ENTITY_FILES.values()
        }
        for filename, endpoints in RELATION_FILES.items():
            path = root / filename
            rows = 0
            missing: Counter[str] = Counter()
            empty: Counter[str] = Counter()
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not ensure_columns(reader, filename, [column for column, _ in endpoints], summary["errors"]):
                    continue
                for row in reader:
                    rows += 1
                    for column, kind in endpoints:
                        value = (row.get(column) or "").strip()
                        if not value:
                            empty[column] += 1
                        elif lookup_statements[kind].execute(
                            f"SELECT 1 FROM {kind} WHERE id = ?", (value,)
                        ).fetchone() is None:
                            missing[column] += 1
                    progress(filename, rows)
            summary["relations"][filename] = {
                "rows": rows,
                "missing_endpoints": dict(missing),
                "empty_endpoints": dict(empty),
            }
            print("relation", filename, summary["relations"][filename], flush=True)

        connection.execute("CREATE TABLE document(id TEXT PRIMARY KEY) WITHOUT ROWID")
        rows = duplicates = empty = missing_service = 0
        path = root / "documents.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if ensure_columns(reader, path.name, ["doc_id", "service_id"], summary["errors"]):
                for row in reader:
                    rows += 1
                    doc_id = (row.get("doc_id") or "").strip()
                    service_id = (row.get("service_id") or "").strip()
                    if not doc_id:
                        empty += 1
                    else:
                        before = connection.total_changes
                        connection.execute("INSERT OR IGNORE INTO document(id) VALUES (?)", (doc_id,))
                        duplicates += int(connection.total_changes == before)
                    if not service_id or connection.execute(
                        "SELECT 1 FROM service WHERE id = ?", (service_id,)
                    ).fetchone() is None:
                        missing_service += 1
                    progress(path.name, rows)
        connection.commit()
        summary["documents"] = {
            "rows": rows,
            "duplicate_doc_ids": duplicates,
            "empty_doc_ids": empty,
            "missing_services": missing_service,
        }
        print("documents", summary["documents"], flush=True)

        chunk_path = root / "documents_chunks.csv"
        if chunk_path.exists():
            connection.execute("CREATE TABLE chunk(id TEXT PRIMARY KEY) WITHOUT ROWID")
            rows = duplicates = empty = missing_document = missing_service = oversize = 0
            with chunk_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = ["chunk_id", "doc_id", "service_id", "content"]
                if ensure_columns(reader, chunk_path.name, required, summary["errors"]):
                    for row in reader:
                        rows += 1
                        chunk_id = (row.get("chunk_id") or "").strip()
                        doc_id = (row.get("doc_id") or "").strip()
                        service_id = (row.get("service_id") or "").strip()
                        if not chunk_id:
                            empty += 1
                        else:
                            before = connection.total_changes
                            connection.execute("INSERT OR IGNORE INTO chunk(id) VALUES (?)", (chunk_id,))
                            duplicates += int(connection.total_changes == before)
                        if not doc_id or connection.execute(
                            "SELECT 1 FROM document WHERE id = ?", (doc_id,)
                        ).fetchone() is None:
                            missing_document += 1
                        if not service_id or connection.execute(
                            "SELECT 1 FROM service WHERE id = ?", (service_id,)
                        ).fetchone() is None:
                            missing_service += 1
                        if len(row.get("content") or "") > args.max_chunk_chars:
                            oversize += 1
                        progress(chunk_path.name, rows)
            summary["chunks"] = {
                "rows": rows,
                "duplicate_chunk_ids": duplicates,
                "empty_chunk_ids": empty,
                "missing_documents": missing_document,
                "missing_services": missing_service,
                f"content_over_{args.max_chunk_chars}": oversize,
            }
            print("chunks", summary["chunks"], flush=True)
        else:
            summary["chunks"] = {"status": "not_present"}

        entity_valid = all(
            not item["duplicate_ids"] and not item["empty_ids"]
            for item in summary["entities"].values()
        )
        relation_valid = all(
            not item["missing_endpoints"] and not item["empty_endpoints"]
            for item in summary["relations"].values()
        )
        document_valid = all(
            not summary["documents"].get(key, 0)
            for key in ("duplicate_doc_ids", "empty_doc_ids", "missing_services")
        )
        chunk_valid = summary["chunks"].get("status") == "not_present" or all(
            not value for key, value in summary["chunks"].items() if key != "rows"
        )
        summary["valid"] = (
            not summary["errors"] and entity_valid and relation_valid and document_valid and chunk_valid
        )
        output = root / "manifests" / "validation_summary.json"
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"VALID {summary['valid']} output {output}", flush=True)
        return 0 if summary["valid"] else 1
    finally:
        connection.close()
        database.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
