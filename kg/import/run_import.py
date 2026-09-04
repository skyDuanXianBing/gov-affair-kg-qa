#!/usr/bin/env python3
"""pilot 骨架层导入器：滚动分片 + 上传 + Builder Job + checkpoint 断点续传。

用法：
  python3 kg/import/run_import.py --dry-run                 # 全量 dry-run（校验+payload 预览）
  python3 kg/import/run_import.py --tables departments       # 单表 dry-run
  python3 kg/import/run_import.py --tables departments --execute --wait
  python3 kg/import/run_import.py --all --execute --wait    # 全量导入（按 manifest 顺序）

协议（schema/openspg_api_import.md 历史版本 + 本机实测）：
  CSV 分片 → POST /public/v1/reasoner/dialog/uploadFile → MinIO fileUrl
  → 按 manifest 覆盖 mappingConfig（schema 元数据取自实时 queryProjectSchema）
  → POST /public/v1/builder/job/submit → GET /public/v1/builder/job/get 轮询

checkpoint：kg/import/checkpoints/state.json，键 = job_key:part_no:sha256；
重跑时 SHA256 命中且状态 SUCCESS 的分片直接跳过，不重复导入。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from spg_client import SpgClient, SpgClientError, load_schema_catalog, resolve_schema_target  # noqa: E402

DEFAULT_MANIFEST = ROOT / "kg" / "import" / "manifests" / "pilot_import_manifest.json"
DEFAULT_STATE = ROOT / "kg" / "import" / "checkpoints" / "state.json"
SHARD_DIR = ROOT / "kg" / "import" / "shards"
PREVIEW_DIR = ROOT / "kg" / "import" / "checkpoints" / "preview"
HASH_CHUNK = 1024 * 1024

IMPORT_ORDER = ["routing", "routing_rel", "shared", "weak", "services", "relations", "service_routing"]


@dataclass
class UploadUnit:
    path: Path
    part_no: int
    sha256: str
    row_count: int | None
    temporary: bool


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def log(message: str) -> None:
    print(message, flush=True)


def sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 分片：完整 CSV 记录边界切分，重复表头；services 追加路由三列
# --------------------------------------------------------------------------

def enrich_service_row(row: dict[str, str]) -> dict[str, str]:
    from generate_routing_metadata import DOMAIN_ID, category_id, model_id

    category = (row.get("category_l2") or "").strip() or "未分类"
    row["domain_id"] = DOMAIN_ID
    row["category_id"] = category_id(category)
    row["model_id"] = model_id(category)
    return row


def enrich_condition_row(row: dict[str, str]) -> dict[str, str]:
    """conditions.csv 的 condition_name 全空（481,500/481,500），
    SPG 实体缺 name 会被 Neo4jSinkWriter 静默丢弃（write Node ignore node）。
    回退：name = condition_text 前 64 字符。"""
    if not (row.get("condition_name") or "").strip():
        text = (row.get("condition_text") or "").strip()
        row["condition_name"] = text[:64]
    return row


ROW_TRANSFORMS = {
    "services": enrich_service_row,
    "conditions": enrich_condition_row,
}


def iter_csv_shards(
    source: Path,
    output_dir: Path,
    *,
    key: str,
    target_bytes: int,
    max_rows: int = 350_000,
    limit_rows: int | None = None,
    enrich=None,
) -> Iterator[UploadUnit]:
    """流式生成 CSV 分片；生成一片 yield 一片（调用方确认成功后删除）。

    双上限：target_bytes（Builder 单文件 ~200MiB 上限）与 max_rows
    （scanner 把整片行载入 JVM/Python 内存，2026-08-30 实测 1.3M 行/片 OOM，
    319K 行/片安全）。短行边表按行数先触上限。
    enrich: 可选的逐行变换（services 补路由三列 / conditions 补 name）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise SpgClientError(f"CSV 无表头: {source}")
        header = [item.strip() for item in header]
        out_fields = list(header)
        if enrich is enrich_service_row:
            for col in ("domain_id", "category_id", "model_id"):
                if col not in out_fields:
                    out_fields.append(col)

        part_no = 0
        rows_total = 0
        while True:
            part_no += 1
            part_path = output_dir / f"{key}-part-{part_no:05d}.csv"
            rows_in_part = 0
            done = False
            with part_path.open("w", encoding="utf-8", newline="") as out:
                writer = csv.writer(out, lineterminator="\n")
                writer.writerow(out_fields)
                for row in reader:
                    if len(row) != len(header):
                        row = (row + [""] * len(header))[: len(header)]
                    record = dict(zip(header, row))
                    if enrich is not None:
                        record = enrich(record)
                    writer.writerow([record.get(field, "") for field in out_fields])
                    rows_in_part += 1
                    rows_total += 1
                    if out.tell() >= target_bytes or rows_in_part >= max_rows:
                        break
                    if limit_rows is not None and rows_total >= limit_rows:
                        done = True
                        break
                bytes_written = out.tell()
            if limit_rows is not None and rows_total >= limit_rows:
                done = True
            if rows_in_part == 0:
                part_path.unlink()
                return
            yield UploadUnit(part_path, part_no, "", rows_in_part, temporary=True)
            if done:
                return


def iter_single_unit(source: Path, *, limit_rows: int | None = None) -> Iterator[UploadUnit]:
    """已废弃兼容入口：统一走 iter_csv_shards 行数分片。"""
    yield from iter_csv_shards(
        source, SHARD_DIR / "trial", key=source.stem,
        target_bytes=128 * 1024 * 1024, max_rows=350_000, limit_rows=limit_rows,
    )


# --------------------------------------------------------------------------
# checkpoint
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "parts": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("parts"), dict):
        raise SpgClientError(f"checkpoint 文件格式错误: {path}")
    return data


STATUS_RANK = {"UPLOADING": 0, "UPLOADED": 1, "SUBMITTED": 2, "SUCCESS": 3}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """锁定后读-合并-写，允许多个导入进程并行更新同一 checkpoint。

    合并规则：同一 part 键取状态更高级的一方（UPLOADING<UPLOADED<SUBMITTED<
    SUCCESS），避免持有旧快照的进程把别的新进度回退；文件中缺失的键按本进程
    记录补入。
    """
    import os
    if os.name == "nt":
        import msvcrt

        def _lock(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

        def _unlock(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def _lock(fh):
            fcntl.flock(fh, fcntl.LOCK_EX)

        def _unlock(fh):
            fcntl.flock(fh, fcntl.LOCK_UN)

    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as lock_fh:
        _lock(lock_fh)
        try:
            merged = load_state(path)
            for key, value in state["parts"].items():
                existing = merged["parts"].get(key)
                if existing is None or STATUS_RANK.get(value.get("status"), -1) >= STATUS_RANK.get(existing.get("status"), -1):
                    merged["parts"][key] = value
            merged["updated_at"] = now_iso()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            state["parts"] = dict(merged["parts"])
        finally:
            _unlock(lock_fh)


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.reader(fh), None)
    if not row:
        raise SpgClientError(f"分片无表头: {path}")
    return [item.strip() for item in row]


def validate_job_mapping(job: dict[str, Any], columns: list[str]) -> None:
    mapping_keys = set(job["mapping"])
    header = set(columns)
    missing = mapping_keys - header
    extra = header - mapping_keys
    if missing or extra:
        raise SpgClientError(
            f"{job['key']} 分片列与 manifest 不一致；缺={sorted(missing)} 多={sorted(extra)}"
        )


def validate_schema_mapping(job: dict[str, Any], schema_target: dict[str, Any]) -> None:
    targets = {t for values in job["mapping"].values() for t in values}
    if schema_target["kind"] == "entity":
        valid = set(schema_target["properties"]) | {"id"}
    else:
        valid = set(schema_target["properties"]) | {"start_id", "end_id"}
    missing = sorted(targets - valid)
    if missing:
        raise SpgClientError(
            f"{job['key']} 映射目标不在实时 Schema: {missing}；"
            f"目标={schema_target.get('qualified_name') or schema_target.get('name')}"
        )


def build_payload(
    manifest: dict[str, Any],
    job: dict[str, Any],
    schema_target: dict[str, Any],
    columns: list[str],
    file_url: str,
    *,
    name_suffix: str,
    file_name: str,
) -> dict[str, Any]:
    if schema_target["kind"] == "entity":
        extension = {
            "mappingConfig": {
                "mappingType": "entityMapping",
                "filter": [{
                    "s": schema_target["qualified_name"],
                    "sId": schema_target["id"],
                    "sZhName": schema_target["name_zh"],
                    "importSchemaCategory": "ENTITY",
                }],
                "config": [{
                    "mapping": deepcopy(job["mapping"]),
                    "name": f"{schema_target['name_zh']}({schema_target['qualified_name']})",
                    "id": "1",
                }],
            }
        }
    else:
        extension = {
            "mappingConfig": {
                "mappingType": "relationMapping",
                "filter": [{
                    "p": schema_target["name"],
                    "pId": schema_target["id"],
                    "pZhName": schema_target["name_zh"],
                    "importSchemaCategory": "RELATION",
                    "s": schema_target["source_type"],
                    "o": schema_target["target_type"],
                }],
                "config": [{
                    "mapping": deepcopy(job["mapping"]),
                    "name": f"{schema_target['name_zh']}({schema_target['name']})",
                    "id": "1",
                }],
            }
        }
    ds = extension.setdefault("dataSourceConfig", {})
    ds.update({
        "columns": [{"name": name, "index": index} for index, name in enumerate(columns)],
        "type": "UPLOAD",
        "fileName": file_name,
        "fileUrl": file_url,
        "ignoreHeader": True,
        "structure": True,
    })
    return {
        "projectId": int(manifest["project_id"]),
        "createUser": manifest.get("create_user", "openspg"),
        "jobName": f"{job.get('name', job['key'])}{name_suffix}",
        "type": "FILE_EXTRACT",
        "dataSourceType": "CSV",
        "fileUrl": file_url,
        "lifeCycle": "ONCE",
        "action": "UPSERT",
        "computingConf": "",
        "extension": json.dumps(extension, ensure_ascii=False, separators=(",", ":")),
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def select_jobs(manifest: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    jobs = list(manifest["jobs"])
    if args.all:
        return jobs
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()]
        known = {job["key"] for job in jobs}
        unknown = [t for t in wanted if t not in known]
        if unknown:
            raise SpgClientError(f"未知任务 key: {unknown}")
        order = {key: i for i, key in enumerate(job["key"] for job in jobs)}
        return sorted([job for job in jobs if job["key"] in wanted], key=lambda j: order[j["key"]])
    if args.group:
        return [job for job in jobs if job.get("group") == args.group]
    raise SpgClientError("请指定 --tables KEY[,KEY]、--group GROUP 或 --all")


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--tables", help="逗号分隔的任务 key 列表")
    parser.add_argument("--group", help="按 manifest group 选择")
    parser.add_argument("--all", action="store_true", help="全部任务（manifest 顺序）")
    parser.add_argument("--execute", action="store_true", help="实际上传并提交；缺省 dry-run")
    parser.add_argument("--wait", action="store_true", help="提交后等待 Builder 完成")
    parser.add_argument("--limit-rows", type=int, help="每表仅导入前 N 行（试跑链路）")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=14400, help="单 Builder Job 等待上限秒")
    parser.add_argument("--target-mib", type=float, default=128.0)
    parser.add_argument("--max-rows-part", type=int, default=350000,
                        help="单片最大行数（scanner 全量载入内存，超限 OOM）")
    parser.add_argument("--name-suffix", default="")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--continue-on-error", action="store_true", help="单表失败记录后继续下一表")
    args = parser.parse_args()

    if args.execute and not args.wait:
        raise SpgClientError("执行导入必须带 --wait（确认 Builder 成功才写 SUCCESS checkpoint）")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    jobs = select_jobs(manifest, args)
    log(
        f"OpenSPG={manifest['base_url']} project={manifest['project_id']} "
        f"namespace={manifest['namespace']} execute={args.execute} jobs={len(jobs)}"
        + (f" limit_rows={args.limit_rows}" if args.limit_rows else "")
    )

    client = SpgClient(manifest["base_url"])
    client.login()
    catalog = client.retry("读取实时 Schema", lambda: load_schema_catalog(client.query_project_schema(manifest["project_id"])))

    state = load_state(args.state_file)
    summary: list[dict[str, Any]] = []
    failures: list[str] = []

    for job_index, job in enumerate(jobs, 1):
        source = (ROOT / job["file"]).resolve()
        if not source.is_file():
            message = f"[{job_index}/{len(jobs)}] {job['key']} 源文件不存在: {source}"
            if args.continue_on_error:
                log(f"SKIP {message}")
                failures.append(message)
                continue
            raise SpgClientError(message)
        size_mib = source.stat().st_size / (1024 * 1024)
        log(f"[{job_index}/{len(jobs)}] {job['key']} <- {job['file']} ({size_mib:.1f} MiB)")

        schema_target = resolve_schema_target(job, manifest["namespace"], catalog)
        validate_schema_mapping(job, schema_target)

        # 全部任务统一走行数+字节双上限分片：小表自然只有 1 片，
        # 短行大表按 max_rows 切，避免 scanner 全量载入 OOM。
        transform = ROW_TRANSFORMS.get(job["key"])
        units = iter_csv_shards(
            source, SHARD_DIR / job["key"],
            key=job["key"], target_bytes=int(args.target_mib * 1024 * 1024),
            max_rows=args.max_rows_part, limit_rows=args.limit_rows, enrich=transform,
        )

        job_record: dict[str, Any] = {"key": job["key"], "group": job.get("group"), "parts": [], "status": "OK"}
        part_no = 0
        try:
            for unit in units:
                part_no = unit.part_no
                columns = read_header(unit.path)
                validate_job_mapping(job, columns)
                digest = sha256sum(unit.path)
                state_key = f"{job['key']}:{part_no:05d}:{digest}"
                prior = state["parts"].get(state_key)
                if prior and prior.get("status") == "SUCCESS" and not args.execute:
                    log(f"  part {part_no:05d} 已成功（checkpoint），dry-run 跳过")
                    job_record["parts"].append({"part": part_no, "status": "SKIPPED"})
                    unit.path.unlink(missing_ok=True) if unit.temporary else None
                    continue
                if prior and prior.get("status") == "SUCCESS":
                    log(f"  part {part_no:05d} SHA256 命中 checkpoint（SUCCESS），跳过导入")
                    job_record["parts"].append({"part": part_no, "status": "SKIPPED"})
                    if unit.temporary:
                        unit.path.unlink(missing_ok=True)
                    continue

                part_suffix = f"{args.name_suffix}-part-{part_no:05d}"
                rows_label = unit.row_count if unit.row_count is not None else "?"
                if not args.execute:
                    payload = build_payload(
                        manifest, job, schema_target, columns,
                        "UPLOAD_URL_AFTER_EXECUTE", name_suffix=part_suffix,
                        file_name=unit.path.name,
                    )
                    preview = PREVIEW_DIR / f"{job['key']}.part-{part_no:05d}.payload.json"
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    printable = deepcopy(payload)
                    try:
                        printable["extension"] = json.loads(printable["extension"])
                    except json.JSONDecodeError:
                        pass
                    preview.write_text(json.dumps(printable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    log(f"  part {part_no:05d} rows={rows_label} bytes={unit.path.stat().st_size:,} [dry-run] payload={preview.relative_to(ROOT)}")
                    job_record["parts"].append({"part": part_no, "rows": unit.row_count, "status": "DRY_RUN"})
                    if unit.temporary:
                        unit.path.unlink(missing_ok=True)
                    continue

                # execute 路径
                state["parts"][state_key] = {
                    "job_key": job["key"], "part_no": part_no, "sha256": digest,
                    "file": str(unit.path), "bytes": unit.path.stat().st_size,
                    "row_count": unit.row_count, "status": "UPLOADING", "updated_at": now_iso(),
                }
                save_state(args.state_file, state)
                file_url = client.retry(
                    f"上传 {unit.path.name}",
                    lambda p=unit.path: client.upload_file(p),
                )
                state["parts"][state_key].update({"status": "UPLOADED", "file_url": file_url, "updated_at": now_iso()})
                save_state(args.state_file, state)
                payload = build_payload(
                    manifest, job, schema_target, columns, file_url,
                    name_suffix=part_suffix, file_name=unit.path.name,
                )
                result = client.retry(
                    "提交 Builder Job",
                    lambda: client.submit_job(payload),
                )
                job_id = result.get("id")
                state["parts"][state_key].update({
                    "status": "SUBMITTED", "job_id": job_id, "updated_at": now_iso(),
                })
                save_state(args.state_file, state)
                log(f"  part {part_no:05d} rows={rows_label} 已提交 Builder Job {job_id}，等待完成 …")
                started = time.time()
                status = client.wait_for_job(
                    int(job_id), interval=args.poll_interval, timeout=args.timeout,
                )
                state["parts"][state_key].update({
                    "status": "SUCCESS", "builder_status": status,
                    "elapsed_seconds": int(time.time() - started), "updated_at": now_iso(),
                })
                save_state(args.state_file, state)
                job_record["parts"].append({
                    "part": part_no, "rows": unit.row_count, "job_id": job_id,
                    "status": status, "seconds": int(time.time() - started),
                })
                log(f"  part {part_no:05d} Builder {status}（{int(time.time() - started)}s）")
                if unit.temporary:
                    unit.path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — 单表失败按策略处理
            job_record["status"] = f"FAILED: {exc}"
            failures.append(f"{job['key']}: {exc}")
            log(f"  ERROR {job['key']}: {exc}")
            if not args.continue_on_error:
                summary.append(job_record)
                break
        summary.append(job_record)

    # 汇总
    report_path = args.state_file.parent / ("import-trial-report.json" if args.limit_rows else "import-report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {"created_at": now_iso(), "execute": args.execute, "limit_rows": args.limit_rows,
             "manifest": str(args.manifest), "jobs": summary, "failures": failures},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    log(f"报告: {report_path}")
    for record in summary:
        parts = record.get("parts") or []
        ok = sum(1 for p in parts if p.get("status") in {"FINISH", "SUCCESS", "SKIPPED"})
        log(f"  {record['key']:<28} {record.get('status','?'):<10} parts={len(parts)} done={ok}")
    if failures:
        log(f"失败 {len(failures)} 个任务：{failures}")
        return 1
    if not args.execute:
        log("当前为 dry-run，未上传、未提交 Builder Job。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (SpgClientError, OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
