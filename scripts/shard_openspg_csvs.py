#!/usr/bin/env python3
"""按 CSV 记录边界切分 OpenSPG 导入文件，并生成可审计 manifest。"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "schema" / "openspg_import_manifest.json"
DEFAULT_OUTPUT = ROOT / "build" / "openspg-shards"
GROUP_ORDER = {"entities": 0, "relations": 1, "documents": 2}


@dataclass
class ShardInfo:
    source_file: str
    part_file: str
    part_no: int
    row_count: int
    bytes: int
    sha256: str
    header: list[str]


def encode_csv_row(row: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerow(row)
    return buffer.getvalue().encode("utf-8")


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def iter_csv_shards(
    source: Path,
    output_dir: Path,
    *,
    target_bytes: int,
    min_free_gib: float = 0.0,
    overwrite: bool = True,
) -> Iterator[ShardInfo]:
    """逐片生成 CSV；调用方处理并删除当前片后，生成器才继续下一片。"""
    source = source.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8-sig", newline="") as input_fh:
        reader = csv.reader(input_fh)
        header = next(reader, None)
        if not header:
            raise ValueError(f"CSV 没有表头: {source}")
        header_bytes = codecs.BOM_UTF8 + encode_csv_row(header)
        if len(header_bytes) >= target_bytes:
            raise ValueError(f"CSV 表头已超过目标分片大小: {source}")

        part_no = 0
        output_fh = None
        output_path: Path | None = None
        digest = hashlib.sha256()
        row_count = 0
        byte_count = 0

        def open_part() -> None:
            nonlocal part_no, output_fh, output_path, digest, row_count, byte_count
            if min_free_gib and disk_free_gib(output_dir) < min_free_gib:
                raise OSError(
                    f"可用空间 {disk_free_gib(output_dir):.2f} GiB 低于下限 {min_free_gib:.2f} GiB"
                )
            part_no += 1
            output_path = output_dir / f"{source.stem}.part-{part_no:05d}.csv"
            if output_path.exists() and not overwrite:
                raise FileExistsError(output_path)
            output_fh = output_path.open("wb")
            output_fh.write(header_bytes)
            digest = hashlib.sha256(header_bytes)
            row_count = 0
            byte_count = len(header_bytes)

        def close_part() -> ShardInfo:
            nonlocal output_fh
            assert output_fh is not None and output_path is not None
            output_fh.flush()
            output_fh.close()
            output_fh = None
            return ShardInfo(
                source_file=str(source),
                part_file=str(output_path.resolve()),
                part_no=part_no,
                row_count=row_count,
                bytes=byte_count,
                sha256=digest.hexdigest(),
                header=list(header),
            )

        open_part()
        try:
            for row in reader:
                encoded = encode_csv_row(row)
                if row_count and byte_count + len(encoded) > target_bytes:
                    yield close_part()
                    open_part()
                assert output_fh is not None
                output_fh.write(encoded)
                digest.update(encoded)
                byte_count += len(encoded)
                row_count += 1
            yield close_part()
        except BaseException:
            if output_fh is not None:
                output_fh.close()
            raise


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("jobs"), list):
        raise ValueError("manifest 缺少 jobs")
    return data


def select_jobs(manifest: dict[str, Any], only: list[str], group: str | None) -> list[dict[str, Any]]:
    jobs = list(manifest["jobs"])
    if only:
        wanted = set(only)
        jobs = [job for job in jobs if job.get("key") in wanted]
        missing = wanted - {job.get("key") for job in jobs}
        if missing:
            raise ValueError(f"未知任务 key: {sorted(missing)}")
    if group:
        jobs = [job for job in jobs if job.get("group") == group]
    return sorted(jobs, key=lambda job: (GROUP_ORDER.get(job.get("group"), 9), manifest["jobs"].index(job)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按完整 CSV 记录切分 OpenSPG 文件")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output-manifest", type=Path)
    parser.add_argument("--target-mib", type=float, default=128.0)
    parser.add_argument("--min-free-gib", type=float, default=15.0)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--group", choices=["entities", "relations", "documents"])
    parser.add_argument("--all", action="store_true", help="明确处理全部任务")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.target_mib <= 0 or args.target_mib >= 200:
        raise ValueError("--target-mib 必须大于 0 且小于 200")
    if not (args.only or args.group or args.all):
        raise ValueError("请显式指定 --only KEY、--group GROUP 或 --all")

    manifest_path = args.manifest.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    jobs = select_jobs(manifest, args.only, args.group)
    output_dir = args.output_dir.expanduser().resolve()
    output_manifest = (args.output_manifest or output_dir / "shard_manifest.json").expanduser().resolve()
    target_bytes = int(args.target_mib * 1024 * 1024)
    report: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "source_manifest": str(manifest_path),
        "target_bytes": target_bytes,
        "min_free_gib": args.min_free_gib,
        "group_order": ["entities", "relations", "documents"],
        "jobs": [],
    }

    for index, job in enumerate(jobs, 1):
        source = Path(job.get("rolling_source_file") or job["file"]).expanduser()
        if not source.is_absolute():
            source = ROOT / source
        if not source.is_file():
            raise FileNotFoundError(source)
        job_dir = output_dir / job["group"] / job["key"]
        if args.overwrite and job_dir.exists():
            shutil.rmtree(job_dir)
        job_report = {"key": job["key"], "group": job["group"], "source_file": str(source.resolve()), "parts": []}
        print(f"[{index}/{len(jobs)}] {job['key']} <- {source}", flush=True)
        if job.get("rolling_transform") == "documents_to_chunks":
            from chunk_openspg_documents import iter_document_chunk_shards

            shards = iter_document_chunk_shards(
                source,
                job_dir,
                target_bytes=target_bytes,
                min_free_gib=args.min_free_gib,
                max_chars=2000,
                overlap_chars=200,
                overwrite=args.overwrite,
            )
        elif job.get("rolling_transform"):
            raise ValueError(f"未知 rolling_transform: {job['rolling_transform']}")
        else:
            shards = iter_csv_shards(
                source,
                job_dir,
                target_bytes=target_bytes,
                min_free_gib=args.min_free_gib,
                overwrite=args.overwrite,
            )
        for shard in shards:
            job_report["parts"].append(asdict(shard))
            print(f"  part={shard.part_no:05d} rows={shard.row_count:,} bytes={shard.bytes:,} sha256={shard.sha256[:12]}...", flush=True)
        report["jobs"].append(job_report)
        atomic_json(output_manifest, report)

    print(f"完成：{output_manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
