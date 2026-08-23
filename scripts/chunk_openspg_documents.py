#!/usr/bin/env python3
"""Create traceable, embedding-safe OpenSPG Chunk rows from document CSV records.

The source CSV is never modified. Each output row represents one semantic retrieval
chunk, retains its parent document ID and source metadata, and uses a deterministic
chunk_id suitable for the OpenSPG entity primary key.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_INPUT = Path("dataset/normalized/openspg/pilot/documents.csv")
DEFAULT_OUTPUT = Path("dataset/normalized/openspg/pilot/documents_chunks.csv")

OUTPUT_FIELDS = [
    "chunk_id",
    "doc_id",
    "chunk_no",
    "chunk_count",
    "title",
    "content",
    "category_l1",
    "category_l2",
    "domain_id",
    "category_id",
    "model_id",
    "service_id",
    "department_name",
    "source_url",
    "source_file",
    "source_line",
    "extras_json",
]

SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；!?;])")
WHITESPACE = re.compile(r"[ \t\f\v]+")
MULTI_NEWLINE = re.compile(r"\n{3,}")


def _safe_component(value: str) -> str:
    value = re.sub(r"\s+", "_", (value or "").strip())
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return value or "uncategorized"


def _model_ids(row: dict[str, str]) -> tuple[str, str, str]:
    """从文档的兼容分类字段生成统一 namespace 的模型过滤字段。"""
    category_l1 = (row.get("category_l1") or "").strip()
    category_l2 = (row.get("category_l2") or "未分类").strip() or "未分类"
    domain_key = "corporate" if category_l1 == "法人服务" else "personal"
    domain_id = f"domain:{domain_key}"
    component = _safe_component(category_l2)
    return domain_id, f"category:{domain_key}:{component}", f"model:{domain_key}:{component}"


@dataclass
class ChunkShardInfo:
    source_file: str
    part_file: str
    part_no: int
    row_count: int
    document_count: int
    bytes: int
    sha256: str
    header: list[str]


def encode_chunk_csv_row(row: dict[str, str]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerow([row.get(field, "") for field in OUTPUT_FIELDS])
    return buffer.getvalue().encode("utf-8")


def iter_document_chunk_shards(
    source: Path,
    output_dir: Path,
    *,
    target_bytes: int,
    min_free_gib: float = 0.0,
    max_chars: int = 2000,
    overlap_chars: int = 200,
    progress_every: int = 10000,
    overwrite: bool = True,
) -> Iterator[ChunkShardInfo]:
    """从 documents.csv 逐片生成 Chunk CSV；调用方消费一片后才继续读取源文件。"""
    if target_bytes <= 0:
        raise ValueError("target_bytes must be greater than 0")
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")

    source = source.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    header_bytes = codecs.BOM_UTF8 + encode_chunk_csv_row(dict(zip(OUTPUT_FIELDS, OUTPUT_FIELDS)))
    if len(header_bytes) >= target_bytes:
        raise ValueError(f"CSV 表头已超过目标分片大小: {source}")

    with source.open("r", encoding="utf-8-sig", newline="") as input_fh:
        reader = csv.DictReader(input_fh)
        missing = {"doc_id", "title", "content"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"input is missing required columns: {', '.join(sorted(missing))}")

        part_no = 0
        output_fh = None
        output_path: Path | None = None
        digest = hashlib.sha256()
        row_count = byte_count = part_document_count = 0
        document_count = chunk_count = max_length = 0
        started_at = time.monotonic()

        def open_part() -> None:
            nonlocal part_no, output_fh, output_path, digest
            nonlocal row_count, byte_count, part_document_count
            free_gib = shutil.disk_usage(output_dir).free / (1024 ** 3)
            if min_free_gib and free_gib < min_free_gib:
                raise OSError(f"可用空间 {free_gib:.2f} GiB 低于下限 {min_free_gib:.2f} GiB")
            part_no += 1
            output_path = output_dir / f"documents_chunks.part-{part_no:05d}.csv"
            if output_path.exists() and not overwrite:
                raise FileExistsError(output_path)
            output_fh = output_path.open("wb")
            output_fh.write(header_bytes)
            digest = hashlib.sha256(header_bytes)
            row_count = 0
            byte_count = len(header_bytes)
            part_document_count = 0

        def close_part() -> ChunkShardInfo:
            nonlocal output_fh
            assert output_fh is not None and output_path is not None
            output_fh.flush()
            output_fh.close()
            output_fh = None
            return ChunkShardInfo(
                source_file=str(source),
                part_file=str(output_path.resolve()),
                part_no=part_no,
                row_count=row_count,
                document_count=part_document_count,
                bytes=byte_count,
                sha256=digest.hexdigest(),
                header=list(OUTPUT_FIELDS),
            )

        open_part()
        try:
            for document in reader:
                document_count += 1
                document_had_chunk = False
                for chunk in chunk_rows([document], max_chars, overlap_chars):
                    encoded = encode_chunk_csv_row(chunk)
                    if row_count and byte_count + len(encoded) > target_bytes:
                        yield close_part()
                        open_part()
                        document_had_chunk = False
                    assert output_fh is not None
                    output_fh.write(encoded)
                    digest.update(encoded)
                    byte_count += len(encoded)
                    row_count += 1
                    chunk_count += 1
                    max_length = max(max_length, len(chunk["content"]))
                    if not document_had_chunk:
                        part_document_count += 1
                        document_had_chunk = True
                if progress_every and document_count % progress_every == 0:
                    assert output_fh is not None
                    output_fh.flush()
                    free_gib = shutil.disk_usage(output_dir).free / (1024 ** 3)
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    print(
                        f"chunk-progress documents={document_count} chunks={chunk_count} "
                        f"part={part_no:05d} rate={document_count / elapsed:.1f}/s "
                        f"free={free_gib:.1f}GiB",
                        flush=True,
                    )
                    if min_free_gib and free_gib < min_free_gib:
                        raise OSError(
                            f"可用空间 {free_gib:.2f} GiB 低于下限 {min_free_gib:.2f} GiB"
                        )
            yield close_part()
            print(
                f"chunk-shards-complete source={source} documents={document_count} "
                f"chunks={chunk_count} parts={part_no} max_content_chars={max_length}",
                flush=True,
            )
        except BaseException:
            if output_fh is not None:
                output_fh.close()
            raise


def normalize_text(value: str) -> str:
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(WHITESPACE.sub(" ", line).strip() for line in value.split("\n"))
    return MULTI_NEWLINE.sub("\n\n", value).strip()


def hard_split(text: str, limit: int) -> Iterable[str]:
    """Split an oversized atomic string without exceeding limit."""
    for start in range(0, len(text), limit):
        yield text[start : start + limit].strip()


def bounded_units(paragraph: str, limit: int) -> list[str]:
    """Prefer sentence boundaries; use a hard cut only for one oversized sentence."""
    if len(paragraph) <= limit:
        return [paragraph]

    units: list[str] = []
    buffer = ""
    sentences = [part.strip() for part in SENTENCE_BOUNDARY.split(paragraph) if part.strip()]
    for sentence in sentences:
        if len(sentence) > limit:
            if buffer:
                units.append(buffer)
                buffer = ""
            units.extend(hard_split(sentence, limit))
            continue
        candidate = f"{buffer}{sentence}" if buffer else sentence
        if len(candidate) <= limit:
            buffer = candidate
        else:
            units.append(buffer)
            buffer = sentence
    if buffer:
        units.append(buffer)
    return units or list(hard_split(paragraph, limit))


def split_content(content: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Create retrieval chunks at paragraph/sentence boundaries with bounded overlap."""
    content = normalize_text(content)
    if not content:
        return []
    if len(content) <= max_chars:
        return [content]

    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        units.extend(bounded_units(paragraph, max_chars))

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            overlap = current[-overlap_chars:].strip() if overlap_chars else ""
            current = f"{overlap}\n\n{unit}".strip() if overlap else unit
        else:
            chunks.extend(hard_split(unit, max_chars))
            current = ""

        # A carry-over overlap plus a long next unit can exceed max_chars.
        if len(current) > max_chars:
            if overlap and current.startswith(overlap):
                current = unit
            if len(current) > max_chars:
                pieces = list(hard_split(current, max_chars))
                chunks.extend(pieces[:-1])
                current = pieces[-1]

    if current:
        chunks.append(current)
    return chunks


def chunk_rows(rows: Iterable[dict[str, str]], max_chars: int, overlap_chars: int):
    for row in rows:
        parent_doc_id = (row.get("doc_id") or "").strip()
        if not parent_doc_id:
            continue
        chunks = split_content(row.get("content") or "", max_chars, overlap_chars)
        total = len(chunks)
        for index, content in enumerate(chunks, start=1):
            item = {field: row.get(field, "") for field in OUTPUT_FIELDS}
            item["chunk_id"] = f"{parent_doc_id}#chunk:{index:04d}"
            item["doc_id"] = parent_doc_id
            item["chunk_no"] = str(index)
            item["chunk_count"] = str(total)
            base_title = (row.get("title") or parent_doc_id).strip()
            item["title"] = f"{base_title}（第{index}/{total}段）"
            item["content"] = content
            item["domain_id"], item["category_id"], item["model_id"] = _model_ids(row)
            yield item


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--overlap-chars", type=int, default=200)
    parser.add_argument("--progress-every", type=int, default=10000)
    parser.add_argument("--min-free-gib", type=float, default=8.0)
    args = parser.parse_args()

    if args.max_chars < 200:
        parser.error("--max-chars must be at least 200")
    if not 0 <= args.overlap_chars < args.max_chars:
        parser.error("--overlap-chars must be >= 0 and smaller than --max-chars")
    if args.progress_every < 0 or args.min_free_gib < 0:
        parser.error("--progress-every and --min-free-gib must be >= 0")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    document_count = chunk_count = max_length = 0
    started_at = time.monotonic()
    try:
        with args.input.open("r", encoding="utf-8-sig", newline="") as source, temporary.open(
            "w", encoding="utf-8", newline=""
        ) as target:
            reader = csv.DictReader(source)
            missing = {"doc_id", "title", "content"} - set(reader.fieldnames or [])
            if missing:
                parser.error(f"input is missing required columns: {', '.join(sorted(missing))}")
            writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                document_count += 1
                for chunk in chunk_rows([row], args.max_chars, args.overlap_chars):
                    writer.writerow(chunk)
                    chunk_count += 1
                    max_length = max(max_length, len(chunk["content"]))
                if args.progress_every and document_count % args.progress_every == 0:
                    target.flush()
                    free_gib = shutil.disk_usage(args.output.parent).free / (1024 ** 3)
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    print(
                        f"progress documents={document_count} chunks={chunk_count} "
                        f"rate={document_count / elapsed:.1f}/s free={free_gib:.1f}GiB",
                        flush=True,
                    )
                    if free_gib < args.min_free_gib:
                        raise RuntimeError(
                            f"free disk space {free_gib:.2f} GiB is below "
                            f"--min-free-gib={args.min_free_gib:.2f}"
                        )
        os.replace(temporary, args.output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    print(
        f"created={args.output} documents={document_count} rows={chunk_count} "
        f"max_content_chars={max_length} max_chars={args.max_chars} "
        f"overlap_chars={args.overlap_chars}"
    )


if __name__ == "__main__":
    main()
