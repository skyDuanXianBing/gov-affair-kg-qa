#!/usr/bin/env python3
"""导入期共享 ID 重写层：材料与法条相关 CSV 的流式重写。

职责（对应《需求规格说明书 2026-08-29》第 1 条）：
1. Material 按规范化名称合并为共享节点，id = `M-{md5(canon_name)[:16]}`；
   material_type/source_type/submission_format 等逐事项字段通过 JOIN
   私有材料行下沉到 requiresMaterial 边，节点只保留共享层列。
2. legal_bases.csv 一行拆为两层：LegalCitation（条款级，
   `LC-{md5(规范化法规名|规范化文号|规范化条款)[:16]}`）与 LegalBasis
   （文号级去重，`LB-{md5(规范化法规名|规范化文号)[:16]}`，文号缺失时
   键退化为法规名称），并生成 citation --partOf--> basis 边。法规名参与
   哈希键：同名不同文号不合并，同文号不同法规名也不合并（源数据存在
   同一修改决定文号同时修改多部法规的真实模式，按文号单独作键会错误
   合并并让条文内容互相覆盖）。
3. service_based_on.csv 的 legal_basis_id 引用同步重写为 LC id，
   原始 id 保留为 source_legal_basis_id 来源追踪列。

输出写入 build/shared_ids/<dataset>/，绝不修改 data/ 原始文件。
全程流式逐行读写；内存中只保留共享节点表与 old_id -> new_id 映射
（pilot legal_bases 约 630 万行未去重，映射约 1.5GB，32GB 内存可承载；
抽样验证请使用 --limit）。仅依赖 Python 3 标准库。

示例：
    python3 scripts/build_shared_ids.py --dataset pilot --limit 2000
    python3 scripts/build_shared_ids.py --dataset both --part materials
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import unicodedata
from datetime import datetime
from itertools import islice
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "data"
DEFAULT_OUT_ROOT = ROOT / "build" / "shared_ids"
DATASETS = ("pilot", "personal")
ID_HASH_LEN = 16
# materials.csv 中属于共享层的列；其余列视为逐事项字段，下沉到边。
MATERIAL_SHARED_COLUMNS = ("material_id", "material_name")
# legal_bases.csv 固定列。
LEGAL_ID_COLUMN = "legal_basis_id"
LEGAL_SHARED_COLUMNS = (
    "law_name",
    "article",
    "document_number",
    "clause_content",
    "published_date",
    "law_url",
)
MATERIALS_FILE = "materials.csv"
MATERIAL_EDGE_FILE = "service_requires_material.csv"
LEGAL_FILE = "legal_bases.csv"
LEGAL_EDGE_FILE = "service_based_on.csv"

csv.field_size_limit(16 * 1024 * 1024)


def canonicalize(text: str) -> str:
    """名称规范化：NFKC 全角归一 + 去除全部空白 + casefold。

    仅用于共享 ID 的哈希键与 canonical_name 输出，不改动展示名称。
    """
    if not text:
        return ""
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def digest16(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:ID_HASH_LEN]


def material_shared_id(canon_name: str) -> str:
    return f"M-{digest16(canon_name)}"


def basis_shared_id(canon_law: str, canon_doc_key: str) -> str:
    return f"LB-{digest16(f'{canon_law}|{canon_doc_key}')}"


def citation_shared_id(canon_law: str, canon_doc_key: str, canon_article: str) -> str:
    return f"LC-{digest16(f'{canon_law}|{canon_doc_key}|{canon_article}')}"


def citation_display_name(law_name: str, article: str) -> str:
    """LegalCitation.name：法规名称 + 条款组成的可读名称。"""
    name = law_name.strip()
    clause = article.strip()
    if name and clause:
        return f"{name} {clause}"
    return name or clause


def open_csv_reader(path: Path):
    """打开 CSV 供流式读取（容忍 BOM），返回 (file, csv.reader)。"""
    handle = path.open("r", newline="", encoding="utf-8-sig")
    return handle, csv.reader(handle)


def open_csv_writer(path: Path):
    """打开 CSV 供流式写入，返回 (file, csv.writer)。"""
    handle = path.open("w", newline="", encoding="utf-8")
    return handle, csv.writer(handle)


def read_header(reader: csv.reader, path: Path) -> tuple[list[str], dict[str, int]]:
    header = next(reader, None)
    if header is None:
        raise SystemExit(f"CSV 缺少表头: {path}")
    return header, {name: i for i, name in enumerate(header)}


def column_index(columns: dict[str, int], name: str, path: Path) -> int:
    if name not in columns:
        raise SystemExit(f"CSV 缺少必需列 {name}: {path}")
    return columns[name]


def process_materials(data_dir: Path, out_dir: Path, limit: int | None) -> dict:
    """重写材料节点表与 requiresMaterial 边表。

    第一遍流式扫描 materials.csv 建立共享节点表和 old_id -> (共享id, 逐事项字段)
    映射；第二遍流式重写 service_requires_material.csv，逐对 JOIN 逐事项字段。
    """
    started = time.perf_counter()
    src_path = data_dir / MATERIALS_FILE
    edge_path = data_dir / MATERIAL_EDGE_FILE
    if not src_path.exists() or not edge_path.exists():
        raise SystemExit(f"数据目录缺少 {MATERIALS_FILE} 或 {MATERIAL_EDGE_FILE}: {data_dir}")

    # ---- 第一遍：materials.csv -> 共享节点 + old_id 映射 ----
    old_to_shared: dict[str, tuple[str, tuple[str, ...]]] = {}
    shared_nodes: dict[str, dict] = {}  # m_id -> {"name", "canonical_name", "merged"}
    pair_intern: dict[tuple[str, ...], tuple[str, ...]] = {}
    input_rows = 0
    empty_name_rows = 0

    handle, reader = open_csv_reader(src_path)
    try:
        header, columns = read_header(reader, src_path)
        id_idx = column_index(columns, "material_id", src_path)
        name_idx = column_index(columns, "material_name", src_path)
        pair_columns = [c for c in header if c not in MATERIAL_SHARED_COLUMNS]
        pair_idx = [columns[c] for c in pair_columns]
        rows = reader if limit is None else islice(reader, limit)
        for row in rows:
            input_rows += 1
            old_id = row[id_idx]
            canon = canonicalize(row[name_idx])
            if not canon:
                # 空名称无法归一，不参与合并，保留原 id 透传到边。
                empty_name_rows += 1
                old_to_shared[old_id] = (old_id, ())
                continue
            m_id = material_shared_id(canon)
            pair = tuple(row[i] for i in pair_idx)
            pair = pair_intern.setdefault(pair, pair)
            if old_id not in old_to_shared:
                old_to_shared[old_id] = (m_id, pair)
                node = shared_nodes.get(m_id)
                if node is None:
                    shared_nodes[m_id] = {
                        "name": row[name_idx],
                        "canonical_name": canon,
                        "merged": 1,
                    }
                else:
                    node["merged"] += 1
    finally:
        handle.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    nodes_path = out_dir / "materials_out.csv"
    with open_csv_writer(nodes_path)[0] as nodes_handle:
        nodes_writer = csv.writer(nodes_handle)
        nodes_writer.writerow(
            ["material_id", "material_name", "canonical_name", "merged_count"]
        )
        for m_id, node in shared_nodes.items():
            nodes_writer.writerow(
                [m_id, node["name"], node["canonical_name"], node["merged"]]
            )

    # ---- 第二遍：service_requires_material.csv -> 重写边 ----
    edge_out_path = out_dir / "service_requires_material_out.csv"
    edge_rows = 0
    edge_unmapped = 0
    edge_handle, edge_reader = open_csv_reader(edge_path)
    try:
        edge_header, edge_columns = read_header(edge_reader, edge_path)
        edge_id_idx = column_index(edge_columns, "material_id", edge_path)
        out_header = list(edge_header) + [
            c for c in pair_columns if c not in edge_header
        ]
        rows = edge_reader if limit is None else islice(edge_reader, limit)
        with open_csv_writer(edge_out_path)[0] as out_handle:
            writer = csv.writer(out_handle)
            writer.writerow(out_header)
            pair_out_idx = {c: out_header.index(c) for c in pair_columns}
            for row in rows:
                edge_rows += 1
                # 逐事项字段列追加在原边表之后，先补齐长度再赋值。
                new_row = list(row)
                if len(new_row) < len(out_header):
                    new_row.extend([""] * (len(out_header) - len(new_row)))
                mapped = old_to_shared.get(row[edge_id_idx])
                if mapped is None:
                    # 抽样模式下常见：边的材料行不在被扫过的前 N 行内。
                    edge_unmapped += 1
                else:
                    new_row[edge_id_idx] = mapped[0]
                    for value, col in zip(mapped[1], pair_columns):
                        new_row[pair_out_idx[col]] = value
                writer.writerow(new_row)
    finally:
        edge_handle.close()

    merge_groups = sum(1 for node in shared_nodes.values() if node["merged"] > 1)
    distinct_ids = len(old_to_shared)
    return {
        "material_input_rows": input_rows,
        "material_distinct_old_ids": distinct_ids,
        "material_shared_nodes": len(shared_nodes),
        "material_merge_groups": merge_groups,
        "material_compression_ratio": (
            round(len(shared_nodes) / distinct_ids, 6) if distinct_ids else None
        ),
        "material_empty_name_rows": empty_name_rows,
        "edge_per_pair_columns": pair_columns,
        "edge_input_rows": edge_rows,
        "edge_unmapped": edge_unmapped,
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }


def process_legal(data_dir: Path, out_dir: Path, limit: int | None) -> dict:
    """拆分 legal_bases.csv 并重写 service_based_on.csv。

    legal_bases.csv 一行（法规 + 条款）拆为 LegalCitation 与 LegalBasis
    两层：citation 按 (规范化法规名|规范化文号|规范化条款) 去重，basis
    按 (规范化法规名|规范化文号) 去重；同名不同文号、同文号不同法规名
    均不合并。同一 citation id 的 clause_content 不一致时保留首见并计数。
    """
    started = time.perf_counter()
    src_path = data_dir / LEGAL_FILE
    edge_path = data_dir / LEGAL_EDGE_FILE
    if not src_path.exists() or not edge_path.exists():
        raise SystemExit(f"数据目录缺少 {LEGAL_FILE} 或 {LEGAL_EDGE_FILE}: {data_dir}")

    # citations: lc_id -> [law_name, article, doc_no, content, pub, url,
    #                       first_src, src_count, lb_id, content_digest]
    citations: dict[str, list] = {}
    # bases: lb_id -> [law_name, doc_no, pub, url, citation_count]
    bases: dict[str, list] = {}
    old_to_lc: dict[str, str] = {}
    input_rows = 0
    doc_fallback_rows = 0
    content_conflicts = 0
    basis_name_conflicts = 0
    basis_field_conflicts = 0

    handle, reader = open_csv_reader(src_path)
    try:
        header, columns = read_header(reader, src_path)
        idx = {
            col: column_index(columns, col, src_path)
            for col in (LEGAL_ID_COLUMN, *LEGAL_SHARED_COLUMNS)
        }
        rows = reader if limit is None else islice(reader, limit)
        for row in rows:
            input_rows += 1
            old_id = row[idx[LEGAL_ID_COLUMN]]
            law_name = row[idx["law_name"]]
            article = row[idx["article"]]
            doc_no = row[idx["document_number"]]
            content = row[idx["clause_content"]]
            pub = row[idx["published_date"]]
            url = row[idx["law_url"]]

            canon_law = canonicalize(law_name)
            canon_doc = canonicalize(doc_no)
            if not canon_doc:
                # 文号缺失：文档键退化为规范化法规名称（法规名已在键中，幂等）。
                canon_doc = canon_law
                doc_fallback_rows += 1
            lb_id = basis_shared_id(canon_law, canon_doc)
            lc_id = citation_shared_id(canon_law, canon_doc, canonicalize(article))
            old_to_lc.setdefault(old_id, lc_id)

            citation = citations.get(lc_id)
            if citation is None:
                citations[lc_id] = [
                    law_name,
                    article,
                    doc_no,
                    content,
                    pub,
                    url,
                    old_id,
                    1,
                    lb_id,
                    digest16(content),
                ]
            else:
                citation[7] += 1
                if citation[9] != digest16(content):
                    # 同一条款引用内容不一致：保留首见，仅计数。
                    content_conflicts += 1

            basis = bases.get(lb_id)
            if basis is None:
                bases[lb_id] = [law_name, doc_no, pub, url, 1]
            else:
                if citation is None:
                    basis[4] += 1
                if canonicalize(basis[0]) != canonicalize(law_name):
                    basis_name_conflicts += 1
                if basis[3] != url or basis[2] != pub:
                    basis_field_conflicts += 1
    finally:
        handle.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    citations_path = out_dir / "legal_citations_out.csv"
    with open_csv_writer(citations_path)[0] as handle_out:
        writer = csv.writer(handle_out)
        writer.writerow(
            [
                "legal_citation_id",
                "name",
                "law_name",
                "article",
                "document_number",
                "clause_content",
                "published_date",
                "law_url",
                "first_source_legal_basis_id",
                "source_id_count",
            ]
        )
        for lc_id, rec in citations.items():
            writer.writerow(
                [lc_id, citation_display_name(rec[0], rec[1]), *rec[:6], rec[6], rec[7]]
            )

    bases_path = out_dir / "legal_bases_out.csv"
    with open_csv_writer(bases_path)[0] as handle_out:
        writer = csv.writer(handle_out)
        writer.writerow(
            [
                "legal_basis_id",
                "law_name",
                "document_number",
                "published_date",
                "law_url",
                "citation_count",
            ]
        )
        for lb_id, rec in bases.items():
            writer.writerow([lb_id, *rec])

    part_of_path = out_dir / "part_of.csv"
    with open_csv_writer(part_of_path)[0] as handle_out:
        writer = csv.writer(handle_out)
        writer.writerow(["legal_citation_id", "legal_basis_id"])
        for lc_id, rec in citations.items():
            writer.writerow([lc_id, rec[8]])

    # ---- service_based_on.csv -> 引用改写为 LC id ----
    edge_out_path = out_dir / "service_based_on_out.csv"
    edge_rows = 0
    edge_unmapped = 0
    edge_handle, edge_reader = open_csv_reader(edge_path)
    try:
        edge_header, edge_columns = read_header(edge_reader, edge_path)
        ref_idx = column_index(edge_columns, LEGAL_ID_COLUMN, edge_path)
        out_header = ["service_id", "legal_citation_id", "order_no", "basis_source", "source_legal_basis_id"]
        rows = edge_reader if limit is None else islice(edge_reader, limit)
        with open_csv_writer(edge_out_path)[0] as out_handle:
            writer = csv.writer(out_handle)
            writer.writerow(out_header)
            service_idx = column_index(edge_columns, "service_id", edge_path)
            order_idx = edge_columns.get("order_no")
            source_idx = edge_columns.get("basis_source")
            for row in rows:
                edge_rows += 1
                old_ref = row[ref_idx]
                lc_id = old_to_lc.get(old_ref)
                if lc_id is None:
                    # 未映射时保留原 id，避免丢边，供人工核查。
                    edge_unmapped += 1
                    lc_id = old_ref
                writer.writerow(
                    [
                        row[service_idx],
                        lc_id,
                        row[order_idx] if order_idx is not None else "",
                        row[source_idx] if source_idx is not None else "",
                        old_ref,
                    ]
                )
    finally:
        edge_handle.close()

    distinct_ids = len(old_to_lc)
    multi_source = sum(1 for rec in citations.values() if rec[7] > 1)
    return {
        "legal_input_rows": input_rows,
        "legal_distinct_old_ids": distinct_ids,
        "legal_citations": len(citations),
        "legal_bases": len(bases),
        "part_of_edges": len(citations),
        "citation_compression_ratio": (
            round(len(citations) / distinct_ids, 6) if distinct_ids else None
        ),
        "citations_multi_source": multi_source,
        "citation_content_conflicts": content_conflicts,
        "basis_law_name_conflicts": basis_name_conflicts,
        "basis_field_conflicts": basis_field_conflicts,
        "basis_doc_fallback_rows": doc_fallback_rows,
        "service_based_on_rows": edge_rows,
        "service_based_on_unmapped": edge_unmapped,
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }


def run_dataset(dataset: str, args: argparse.Namespace) -> dict:
    data_dir = Path(args.data_root) / dataset
    out_dir = Path(args.out_root) / dataset
    # stats.json 按部分增量合并：--part materials 与 --part legal 可分两次跑，
    # 统计互相保留，便于全量材料 + 抽样法条等组合执行方式。
    stats_path = out_dir / "stats.json"
    stats: dict = {}
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stats = {}
    stats.update(
        {
            "dataset": dataset,
            "generated_at": datetime.now().astimezone().isoformat(),
            "limit": args.limit,
        }
    )
    parts = list(stats.get("parts", []))
    if args.part in ("materials", "both"):
        print(f"[{dataset}] 重写材料部分 -> {out_dir}", flush=True)
        stats["materials"] = process_materials(data_dir, out_dir, args.limit)
        stats["materials"]["limit"] = args.limit
        if "materials" not in parts:
            parts.append("materials")
    if args.part in ("legal", "both"):
        print(f"[{dataset}] 重写法条部分 -> {out_dir}", flush=True)
        stats["legal"] = process_legal(data_dir, out_dir, args.limit)
        stats["legal"]["limit"] = args.limit
        if "legal" not in parts:
            parts.append("legal")
    stats["parts"] = parts
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[{dataset}] 统计已写入 {stats_path}", flush=True)
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 Material/LegalBasis/LegalCitation 共享 ID 并重写相关 CSV（不修改 data/ 原文件）"
    )
    parser.add_argument(
        "--dataset",
        choices=("pilot", "personal", "both"),
        default="both",
        help="处理哪个数据目录（默认 both）",
    )
    parser.add_argument(
        "--part",
        choices=("materials", "legal", "both"),
        default="both",
        help="只跑材料部分或法条部分（默认 both）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="抽样模式：每个输入 CSV 最多读取前 N 个数据行（注意：边表引用可能落在 N 行之外，计入 unmapped）",
    )
    parser.add_argument(
        "--data-root",
        default=str(DEFAULT_DATA_ROOT),
        help="数据根目录（默认 <repo>/data）",
    )
    parser.add_argument(
        "--out-root",
        default=str(DEFAULT_OUT_ROOT),
        help="输出根目录（默认 <repo>/build/shared_ids）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    datasets = DATASETS if args.dataset == "both" else (args.dataset,)
    all_stats = []
    for dataset in datasets:
        all_stats.append(run_dataset(dataset, args))
    print(json.dumps(all_stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
