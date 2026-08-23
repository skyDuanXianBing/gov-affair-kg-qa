#!/usr/bin/env python3
"""流式上传规则化 CSV/分片并提交 OpenSPG Builder Job。

默认仅 dry-run；显式传入 --execute 才上传和提交。支持预生成分片、滚动单片、
失败重试和按 SHA256 断点续传。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import sys
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

try:
    from tqdm.auto import tqdm
except ModuleNotFoundError:  # 允许 --no-progress 和单元测试在未安装依赖时运行。
    tqdm = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "schema" / "openspg_import_manifest.json"
DEFAULT_STATE = ROOT / "build" / "openspg-import-state.json"
TERMINAL_SUCCESS = {"FINISH", "SUCCESS", "SUCCEEDED"}
TERMINAL_FAILURE = {
    "FAIL", "FAILED", "ERROR", "CANCELED", "CANCELLED",
    "TERMINATE", "TERMINATED", "STOP", "STOPPED", "ABORTED",
}
GROUP_ORDER = {"entities": 0, "relations": 1, "documents": 2}
RETRIEVAL_REQUIRED_ENTITY_TYPES = frozenset({"Chunk"})
HASH_CHUNK_SIZE = 1024 * 1024
MIN_UPLOAD_CHUNK_SIZE = 64 * 1024
T = TypeVar("T")


class ImportErrorWithContext(RuntimeError):
    pass


class NullProgress:
    """在显式关闭进度条时提供与 tqdm 兼容的最小接口。"""

    def __enter__(self) -> "NullProgress":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def update(self, _: int | float = 1) -> None:
        return None

    def set_description_str(self, _: str, refresh: bool = True) -> None:
        return None

    def set_postfix_str(self, _: str, refresh: bool = True) -> None:
        return None

    def close(self) -> None:
        return None


def create_progress(*, enabled: bool, **kwargs: Any) -> Any:
    """创建 tqdm 进度条；关闭进度时不要求安装 tqdm。"""
    if not enabled:
        return NullProgress()
    if tqdm is None:
        raise ImportErrorWithContext(
            "缺少 tqdm，请先执行：python3 -m pip install -r requirements-import.txt；"
            "或临时使用 --no-progress"
        )
    return tqdm(**kwargs)


def progress_log(message: str, *, progress_enabled: bool) -> None:
    """输出不会破坏活动 tqdm 进度条的日志。"""
    if progress_enabled and tqdm is not None:
        tqdm.write(message)
        return
    print(message, flush=True)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ImportErrorWithContext(f"HTTP {exc.code} {url}: {detail}") from exc
    except URLError as exc:
        raise ImportErrorWithContext(f"请求失败 {url}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportErrorWithContext(f"接口未返回 JSON {url}: {raw[:500]}") from exc
    # queryProjectSchema 返回的是裸 Schema 对象；其余公开接口返回 success 包装。
    if "success" in data and not data.get("success"):
        raise ImportErrorWithContext(f"接口执行失败 {url}: {data.get('errorMsg') or data}")
    return data


def multipart_chunks(
    prefix: bytes,
    path: Path,
    suffix: bytes,
    chunk_size: int,
    *,
    progress: Any | None = None,
) -> Iterator[bytes]:
    """生成 multipart 请求体，并按实际读取的文件字节更新上传进度。"""
    yield prefix
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            if progress is not None:
                progress.update(len(block))
            yield block
    yield suffix


def upload_file(
    base_url: str,
    path: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 600,
    chunk_size: int = HASH_CHUNK_SIZE,
    progress_enabled: bool = False,
    progress_position: int = 2,
) -> str:
    """使用 iterable body 流式上传文件，并显示字节速度与预计剩余时间。"""
    boundary = "----OpenSPGBatch" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    file_size = path.stat().st_size
    content_length = len(prefix) + file_size + len(suffix)
    request_headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(content_length),
        **(headers or {}),
    }
    url = urljoin(base_url.rstrip("/") + "/", "public/v1/reasoner/dialog/uploadFile")
    with create_progress(
        enabled=progress_enabled,
        total=file_size,
        desc=f"上传 {path.name}",
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        dynamic_ncols=True,
        leave=False,
        position=progress_position,
    ) as progress:
        body = multipart_chunks(prefix, path, suffix, chunk_size, progress=progress)
        req = Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ImportErrorWithContext(f"上传失败 HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise ImportErrorWithContext(f"上传失败 {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportErrorWithContext(f"上传接口未返回 JSON: {raw[:500]}") from exc
    if not data.get("success") or not data.get("result"):
        raise ImportErrorWithContext(f"上传失败 {path}: {data}")
    return str(data["result"])


def retry_call(
    label: str,
    fn: Callable[[], T],
    retries: int,
    backoff: float,
    *,
    progress_enabled: bool = False,
) -> T:
    """按指数退避重试，并通过 tqdm.write 输出不破坏进度条的错误。"""
    last: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return fn()
        except (ImportErrorWithContext, OSError) as exc:
            last = exc
            if attempt > retries:
                raise
            delay = backoff * (2 ** (attempt - 1))
            progress_log(
                f"    {label}失败（{attempt}/{retries + 1}）：{exc}；{delay:.1f}s 后重试",
                progress_enabled=progress_enabled,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def read_csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.reader(fh), None)
    if not row:
        raise ImportErrorWithContext(f"CSV 没有表头或内容为空: {path}")
    columns = [item.strip().lstrip("#") for item in row]
    if any(not item for item in columns):
        raise ImportErrorWithContext(f"CSV 表头包含空列名: {path}: {columns}")
    if len(set(columns)) != len(columns):
        raise ImportErrorWithContext(f"CSV 表头存在重复列: {path}: {columns}")
    return columns


def sha256sum(
    path: Path,
    *,
    progress_enabled: bool = False,
    progress_position: int = 2,
) -> str:
    """流式计算文件 SHA256，并可显示校验字节进度。"""
    digest = hashlib.sha256()
    with create_progress(
        enabled=progress_enabled,
        total=path.stat().st_size,
        desc=f"校验 {path.name}",
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        dynamic_ncols=True,
        leave=False,
        position=progress_position,
    ) as progress:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(HASH_CHUNK_SIZE), b""):
                digest.update(block)
                progress.update(len(block))
    return digest.hexdigest()


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"jobs"}
    missing = required - set(data)
    if missing:
        raise ImportErrorWithContext(f"manifest 缺少字段: {sorted(missing)}")
    data["base_url"] = str(data.get("base_url") or "").strip()
    data["project_id"] = int(data.get("project_id") or 0)
    data["namespace"] = str(data.get("namespace") or "").strip()
    if not data["project_id"] or not data["namespace"]:
        raise ImportErrorWithContext("manifest 必须提供 project_id 和 namespace")
    return data


def normalize_retrieval_ids(value: Any, job_key: str) -> list[int]:
    """将 Builder Job 的检索索引配置规范化为正整数 ID 列表。"""
    if value in (None, "", []):
        return []
    raw_value = value
    if isinstance(value, str):
        try:
            raw_value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ImportErrorWithContext(
                f"{job_key} retrievals 必须是 JSON 数组，例如 [1]"
            ) from exc
    if not isinstance(raw_value, (list, tuple)):
        raise ImportErrorWithContext(f"{job_key} retrievals 必须是数组: {value!r}")

    normalized: list[int] = []
    for item in raw_value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ImportErrorWithContext(f"{job_key} retrievals 含有非法索引 ID: {item!r}")
        try:
            retrieval_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ImportErrorWithContext(
                f"{job_key} retrievals 含有非法索引 ID: {item!r}"
            ) from exc
        if retrieval_id <= 0:
            raise ImportErrorWithContext(
                f"{job_key} retrievals 必须使用正整数 ID: {retrieval_id}"
            )
        if retrieval_id not in normalized:
            normalized.append(retrieval_id)
    return normalized


def validate_retrieval_config(jobs: Iterable[dict[str, Any]]) -> None:
    """阻止 Chunk 任务在没有 Retrieval 索引时被提交。"""
    for job in jobs:
        schema_target = job.get("schema_target") or {}
        if schema_target.get("kind") != "entity":
            continue
        entity_type = str(schema_target.get("type") or "")
        if entity_type not in RETRIEVAL_REQUIRED_ENTITY_TYPES:
            continue
        retrieval_ids = normalize_retrieval_ids(job.get("retrievals"), job.get("key", "unknown"))
        if not retrieval_ids:
            raise ImportErrorWithContext(
                f"{job.get('key')} 是 {entity_type} 文档任务，必须配置 retrievals；"
                "否则提交后不会装配文本/向量检索器"
            )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "parts": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("parts"), dict):
        raise ImportErrorWithContext(f"状态文件格式错误: {path}")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = args.token or os.getenv("OPENSPG_TOKEN")
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        raw_token = token.removeprefix("Bearer ").removeprefix("bearer ")
        headers["Cookie"] = f"OPEN_SPG_TOKEN={raw_token}"
    if args.cookie:
        headers["Cookie"] = args.cookie
    return headers


def get_template(base_url: str, template_job_id: int, headers: dict[str, str]) -> dict[str, Any]:
    data = json_request(
        base_url, "/public/v1/builder/job/get", params={"id": template_job_id}, headers=headers
    )
    result = data.get("result")
    if not isinstance(result, dict):
        raise ImportErrorWithContext(f"模板任务不存在: {template_job_id}")
    return result


def _type_name(value: dict[str, Any] | None) -> str:
    """从 OpenSPG Schema 引用中取英文类型名。"""
    if not isinstance(value, dict):
        return ""
    basic = value.get("basicInfo") or {}
    name = basic.get("name") or {}
    return str(name.get("nameEn") or name.get("name") or "")


def _qualified_type_name(value: dict[str, Any] | None) -> str:
    """生成 Builder mappingConfig 所需的 namespace.Type 名称。"""
    if not isinstance(value, dict):
        return ""
    basic = value.get("basicInfo") or {}
    name = basic.get("name") or {}
    namespace = str(name.get("namespace") or "")
    type_name = _type_name(value)
    return f"{namespace}.{type_name}" if namespace and type_name else type_name


def load_schema_catalog(
    base_url: str, project_id: int, headers: dict[str, str]
) -> dict[str, Any]:
    """读取真实项目 Schema，避免依赖已删除或重建后的 Builder 模板任务。"""
    data = json_request(
        base_url,
        "/public/v1/schema/queryProjectSchema",
        params={"projectId": project_id},
        headers=headers,
        timeout=120,
    )
    types = data.get("spgTypes")
    if not isinstance(types, list):
        raise ImportErrorWithContext(f"项目 {project_id} 没有可解析的 Schema")

    entities: dict[str, dict[str, Any]] = {}
    relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in types:
        if not isinstance(item, dict):
            continue
        basic = item.get("basicInfo") or {}
        name = basic.get("name") or {}
        namespace = str(name.get("namespace") or "")
        type_name = _type_name(item)
        if namespace and type_name and item.get("spgTypeEnum") == "ENTITY_TYPE":
            entity = {
                "name": type_name,
                "qualified_name": f"{namespace}.{type_name}",
                "name_zh": str(basic.get("nameZh") or type_name),
                "id": (item.get("ontologyId") or {}).get("uniqueId"),
                "properties": set(),
            }
            for prop in item.get("properties") or []:
                prop_name = str((prop.get("basicInfo") or {}).get("name", {}).get("name") or "")
                if prop_name:
                    entity["properties"].add(prop_name)
            entities[type_name] = entity
            for relation in item.get("relations") or []:
                rel_name = str((relation.get("basicInfo") or {}).get("name", {}).get("name") or "")
                target = _qualified_type_name(relation.get("objectTypeRef"))
                if not rel_name or not target:
                    continue
                relation_item = {
                    "name": rel_name,
                    "name_zh": str((relation.get("basicInfo") or {}).get("nameZh") or rel_name),
                    "id": (relation.get("ontologyId") or {}).get("uniqueId"),
                    "source_type": f"{namespace}.{type_name}",
                    "target_type": target,
                    "properties": {
                        str((prop.get("basicInfo") or {}).get("name", {}).get("name"))
                        for prop in (relation.get("advancedConfig") or {}).get("subProperties") or []
                        if (prop.get("basicInfo") or {}).get("name", {}).get("name")
                    },
                }
                relations[(type_name, rel_name, target.rsplit(".", 1)[-1])] = relation_item
    if not entities:
        raise ImportErrorWithContext(f"项目 {project_id} 没有找到实体 Schema")
    return {"entities": entities, "relations": relations}


def resolve_schema_target(
    job: dict[str, Any], manifest: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    """把 manifest 的语义类型解析为当前项目的 ontology 元数据。"""
    target = job.get("schema_target") or {}
    kind = target.get("kind")
    if kind == "entity":
        type_name = str(target.get("type") or "")
        entity = catalog["entities"].get(type_name)
        if not entity:
            raise ImportErrorWithContext(f"{job['key']} 对应实体不存在: {type_name}")
        return {"kind": kind, **entity}
    if kind == "relation":
        source = str(target.get("source_type") or "")
        relation = str(target.get("relation") or "")
        target_type = str(target.get("target_type") or "")
        source_short = source.rsplit(".", 1)[-1]
        target_short = target_type.rsplit(".", 1)[-1]
        if "." not in source:
            source = f"{manifest['namespace']}.{source}"
        if "." not in target_type:
            target_type = f"{manifest['namespace']}.{target_type}"
        relation_item = catalog["relations"].get((source_short, relation, target_short))
        if not relation_item:
            raise ImportErrorWithContext(
                f"{job['key']} 对应关系不存在: {source}.{relation}->{target_type}"
            )
        return {"kind": kind, **relation_item}
    raise ImportErrorWithContext(f"{job['key']} 缺少有效 schema_target.kind")


def validate_job(job: dict[str, Any], columns: list[str]) -> None:
    mapping = job.get("mapping")
    if not isinstance(mapping, dict):
        raise ImportErrorWithContext(f"{job.get('key')} mapping 必须是对象")
    header_set = set(columns)
    mapping_set = set(mapping)
    missing = mapping_set - header_set
    unmapped = header_set - mapping_set
    if missing or unmapped:
        raise ImportErrorWithContext(
            f"{job.get('key')} CSV/manifest 列不一致；CSV缺少={sorted(missing)}，manifest缺少={sorted(unmapped)}"
        )
    targets = [target for values in mapping.values() for target in values]
    if targets.count("id") > 1 or targets.count("start_id") > 1 or targets.count("end_id") > 1:
        raise ImportErrorWithContext(f"{job.get('key')} 主键或关系端点被重复映射")


def validate_schema_mapping(job: dict[str, Any], schema_target: dict[str, Any]) -> None:
    """确认 manifest 的目标属性都存在于实时 Schema。"""
    mapping = job["mapping"]
    targets = {target for values in mapping.values() for target in values}
    if schema_target["kind"] == "entity":
        valid = set(schema_target["properties"]) | {"id"}
        missing = sorted(targets - valid)
    else:
        valid = set(schema_target["properties"]) | {"start_id", "end_id"}
        missing = sorted(targets - valid)
    if missing:
        raise ImportErrorWithContext(
            f"{job['key']} 映射目标不在实时 Schema 中: {missing}; "
            f"目标={schema_target.get('qualified_name') or schema_target.get('name')}"
        )


def build_payload(
    manifest: dict[str, Any],
    job: dict[str, Any],
    template: dict[str, Any] | None,
    schema_target: dict[str, Any],
    columns: list[str],
    file_url: str,
    *,
    mode: str,
    name_suffix: str,
    file_name: str | None = None,
) -> dict[str, Any]:
    if template is not None:
        try:
            extension = json.loads(template["extension"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ImportErrorWithContext(f"模板任务 extension 无效: {job.get('template_job_id')}") from exc
    elif schema_target["kind"] == "entity":
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
        "fileName": file_name or Path(job["file"]).name,
        "fileUrl": file_url,
        "ignoreHeader": True,
        "structure": True,
    })
    mapping_config = extension.get("mappingConfig")
    if not isinstance(mapping_config, dict) or not mapping_config.get("config"):
        raise ImportErrorWithContext(f"{job['key']} 不含结构化 mappingConfig")
    mapping_config["config"][0]["mapping"] = deepcopy(job["mapping"])

    template_namespace = ""
    filters = mapping_config.get("filter") or []
    if filters:
        template_namespace = str(filters[0].get("s") or "").split(".", 1)[0]
    if template_namespace and template_namespace != manifest["namespace"]:
        raise ImportErrorWithContext(
            f"模板 namespace={template_namespace}，manifest namespace={manifest['namespace']}"
        )

    template = template or {}
    payload: dict[str, Any] = {
        "projectId": int(manifest["project_id"]),
        "createUser": manifest.get("create_user", template.get("createUser", "openspg")),
        "jobName": f"{job.get('name', job['key'])}{name_suffix}",
        "type": template.get("type", "FILE_EXTRACT"),
        "dataSourceType": "CSV",
        "fileUrl": file_url,
        "lifeCycle": template.get("lifeCycle") or "ONCE",
        "action": template.get("action") or "UPSERT",
        "computingConf": template.get("computingConf") or "",
        "extension": json.dumps(extension, ensure_ascii=False, separators=(",", ":")),
    }
    retrieval_value = (
        job["retrievals"] if "retrievals" in job else template.get("retrievals")
    )
    retrieval_ids = normalize_retrieval_ids(retrieval_value, job.get("key", "unknown"))
    if schema_target.get("kind") == "entity" and schema_target.get("name") in RETRIEVAL_REQUIRED_ENTITY_TYPES:
        if not retrieval_ids:
            raise ImportErrorWithContext(
                f"{job.get('key')} 是 Chunk 文档任务，但没有有效 retrievals；已拒绝提交"
            )
    if retrieval_ids:
        payload["retrievals"] = json.dumps(retrieval_ids)
    if mode == "update" and job.get("template_job_id"):
        payload["id"] = int(job["template_job_id"])
    return payload


def select_jobs(manifest: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    indexed = list(enumerate(manifest["jobs"]))
    wanted = set(args.only or [])
    if wanted:
        indexed = [(i, job) for i, job in indexed if job["key"] in wanted]
        unknown = wanted - {job["key"] for _, job in indexed}
        if unknown:
            raise ImportErrorWithContext(f"未知任务 key: {sorted(unknown)}")
    if args.group:
        indexed = [(i, job) for i, job in indexed if job.get("group") == args.group]
    if not indexed:
        raise ImportErrorWithContext("没有选中任何任务")
    if args.execute and not (args.only or args.group or args.all):
        raise ImportErrorWithContext("批量执行时请显式指定 --only KEY、--group GROUP 或 --all")
    if args.all or args.shard_manifest or args.rolling_shards:
        indexed.sort(key=lambda pair: (GROUP_ORDER.get(pair[1].get("group"), 9), pair[0]))
    return [job for _, job in indexed]


def wait_for_job(
    base_url: str,
    job_id: int,
    headers: dict[str, str],
    interval: int,
    timeout: int,
    *,
    progress_enabled: bool = False,
    progress_position: int = 2,
) -> str:
    """轮询 Builder Job，并以超时时间为上限展示等待进度。"""
    started_at = time.time()
    deadline = started_at + timeout
    previous_status = None
    displayed_seconds = 0
    with create_progress(
        enabled=progress_enabled,
        total=timeout,
        desc=f"等待 Builder {job_id}",
        unit="s",
        dynamic_ncols=True,
        leave=False,
        position=progress_position,
    ) as progress:
        while time.time() < deadline:
            data = json_request(
                base_url, "/public/v1/builder/job/get", params={"id": job_id}, headers=headers
            )["result"]
            status = str(data.get("status") or "UNKNOWN").upper()
            elapsed_seconds = min(timeout, max(0, int(time.time() - started_at)))
            if elapsed_seconds > displayed_seconds:
                progress.update(elapsed_seconds - displayed_seconds)
                displayed_seconds = elapsed_seconds
            progress.set_postfix_str(f"status={status}")
            if status != previous_status:
                progress_log(
                    f"    Builder Job {job_id} 状态: {status}",
                    progress_enabled=progress_enabled,
                )
                previous_status = status
            if status in TERMINAL_SUCCESS:
                return status
            if status in TERMINAL_FAILURE:
                raise ImportErrorWithContext(f"Builder Job {job_id} 执行失败: {status}")
            time.sleep(interval)
    raise ImportErrorWithContext(f"等待 Builder Job {job_id} 超时（{timeout}s）")


def write_preview(preview_dir: Path, key: str, payload: dict[str, Any]) -> Path:
    preview_dir.mkdir(parents=True, exist_ok=True)
    target = preview_dir / f"{key}.payload.json"
    printable = deepcopy(payload)
    try:
        printable["extension"] = json.loads(printable["extension"])
    except Exception:
        pass
    target.write_text(json.dumps(printable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_shard_index(path: Path) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[dict[str, Any]]] = {}
    for job in data.get("jobs", []):
        result[str(job["key"])] = list(job.get("parts", []))
    return result


def prebuilt_units(job: dict[str, Any], shard_index: dict[str, list[dict[str, Any]]]) -> Iterator[dict[str, Any]]:
    parts = shard_index.get(job["key"])
    if parts is None:
        raise ImportErrorWithContext(f"分片 manifest 不含任务: {job['key']}")
    for part in parts:
        path = absolute_path(part["part_file"])
        yield {
            "path": path,
            "part_no": int(part["part_no"]),
            "row_count": int(part.get("row_count", 0)),
            "bytes": int(part.get("bytes", path.stat().st_size if path.exists() else 0)),
            "sha256": str(part["sha256"]),
            "temporary": False,
        }


def rolling_source(job: dict[str, Any]) -> Path:
    return absolute_path(job.get("rolling_source_file") or job["file"])


def rolling_units(job: dict[str, Any], args: argparse.Namespace) -> Iterator[dict[str, Any]]:
    source = rolling_source(job)
    target_bytes = int(args.target_mib * 1024 * 1024)
    output_dir = args.shard_dir.expanduser().resolve() / job["group"] / job["key"]
    transform = job.get("rolling_transform")
    if transform == "documents_to_chunks":
        from chunk_openspg_documents import iter_document_chunk_shards

        parts = iter_document_chunk_shards(
            source,
            output_dir,
            target_bytes=target_bytes,
            min_free_gib=args.min_free_gib,
            max_chars=args.chunk_max_chars,
            overlap_chars=args.chunk_overlap_chars,
            progress_every=args.chunk_progress_every,
            overwrite=True,
        )
    elif transform:
        raise ImportErrorWithContext(f"未知 rolling_transform: {transform}")
    else:
        from shard_openspg_csvs import iter_csv_shards

        parts = iter_csv_shards(
            source,
            output_dir,
            target_bytes=target_bytes,
            min_free_gib=args.min_free_gib,
            overwrite=True,
        )
    for part in parts:
        yield {
            "path": Path(part.part_file),
            "part_no": part.part_no,
            "row_count": part.row_count,
            "bytes": part.bytes,
            "sha256": part.sha256,
            "temporary": True,
        }


def single_unit(job: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path = absolute_path(job["file"])
    yield {
        "path": path,
        "part_no": 1,
        "row_count": None,
        "bytes": path.stat().st_size if path.exists() else 0,
        "sha256": None,
        "temporary": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 OpenSPG HTTP API 流式导入 CSV（默认 dry-run）")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--base-url", help="覆盖 manifest 中的 OpenSPG 地址；也可使用 OPENSPG_BASE_URL")
    parser.add_argument("--project-id", type=int, help="覆盖 manifest 中的 project_id；也可使用 OPENSPG_PROJECT_ID")
    parser.add_argument("--namespace", help="覆盖 manifest 中的 namespace；也可使用 OPENSPG_NAMESPACE")
    parser.add_argument("--only", action="append", help="只处理指定任务 key，可重复")
    parser.add_argument("--group", choices=["documents", "entities", "relations"])
    parser.add_argument("--all", action="store_true", help="执行全部任务；顺序固定为实体、关系、Chunk")
    parser.add_argument("--execute", action="store_true", help="实际上传并提交；缺省仅预览")
    parser.add_argument("--mode", choices=["clone", "update"], default="clone")
    parser.add_argument("--wait", action="store_true", help="每个分片提交后等待任务结束")
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--upload-timeout", type=int, default=1800)
    parser.add_argument("--upload-chunk-mib", type=float, default=1.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=2.0)
    parser.add_argument("--token", help="OPEN_SPG_TOKEN，也可使用同名环境变量")
    parser.add_argument("--cookie", help="完整 Cookie 字符串")
    parser.add_argument("--name-suffix", default="")
    parser.add_argument("--preview-dir", type=Path, default=ROOT / "build" / "openspg-import-preview")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--no-resume", action="store_true", help="不跳过状态文件中已提交/成功的同 SHA 分片")
    parser.add_argument("--shard-manifest", type=Path, help="使用 shard_openspg_csvs.py 预生成的 manifest")
    parser.add_argument("--rolling-shards", action="store_true", help="一次只生成一个分片，处理后立即删除")
    parser.add_argument("--shard-dir", type=Path, default=ROOT / "build" / "openspg-rolling-shards")
    parser.add_argument("--target-mib", type=float, default=128.0)
    parser.add_argument("--min-free-gib", type=float, default=15.0)
    parser.add_argument("--chunk-max-chars", type=int, default=2000)
    parser.add_argument("--chunk-overlap-chars", type=int, default=200)
    parser.add_argument("--chunk-progress-every", type=int, default=10000)
    parser.add_argument("--delete-successful-shards", action="store_true")
    parser.add_argument("--no-progress", action="store_true", help="关闭 tqdm 进度条，适用于纯日志环境")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    progress_enabled = not args.no_progress
    if progress_enabled and tqdm is None:
        create_progress(enabled=True)
    if args.shard_manifest and args.rolling_shards:
        raise ImportErrorWithContext("--shard-manifest 与 --rolling-shards 只能选择一个")
    if args.target_mib <= 0 or args.target_mib >= 200:
        raise ImportErrorWithContext("--target-mib 必须大于 0 且小于 200")
    if args.upload_chunk_mib <= 0:
        raise ImportErrorWithContext("--upload-chunk-mib 必须大于 0")
    if args.chunk_max_chars < 200:
        raise ImportErrorWithContext("--chunk-max-chars 必须至少为 200")
    if not 0 <= args.chunk_overlap_chars < args.chunk_max_chars:
        raise ImportErrorWithContext("--chunk-overlap-chars 必须大于等于 0 且小于 --chunk-max-chars")
    if args.chunk_progress_every < 0:
        raise ImportErrorWithContext("--chunk-progress-every 必须大于等于 0")
    if args.rolling_shards and args.execute and not args.wait:
        raise ImportErrorWithContext("滚动分片执行要求同时使用 --wait，成功确认后才能删除当前分片")
    if args.delete_successful_shards and not (args.execute and args.wait):
        raise ImportErrorWithContext("删除成功分片要求同时使用 --execute --wait")

    manifest_path = args.manifest.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    base_url = (
        args.base_url
        or os.getenv("OPENSPG_BASE_URL")
        or manifest.get("base_url")
    ).rstrip("/")
    if not base_url:
        raise ImportErrorWithContext("请通过 --base-url 或 OPENSPG_BASE_URL 提供 OpenSPG 地址")
    manifest["project_id"] = int(
        args.project_id or os.getenv("OPENSPG_PROJECT_ID") or manifest["project_id"]
    )
    manifest["namespace"] = str(
        args.namespace or os.getenv("OPENSPG_NAMESPACE") or manifest["namespace"]
    )
    headers = auth_headers(args)
    jobs = select_jobs(manifest, args)
    validate_retrieval_config(jobs)
    if args.execute and (args.shard_manifest or args.rolling_shards):
        selected_groups = {job.get("group") for job in jobs}
        if len(selected_groups) > 1 and not args.wait:
            raise ImportErrorWithContext("跨实体/关系/Chunk 分组执行时必须使用 --wait，保证依赖顺序")

    state_path = args.state_file.expanduser().resolve()
    state = load_state(state_path)
    shard_index = (
        load_shard_index(args.shard_manifest.expanduser().resolve())
        if args.shard_manifest
        else {}
    )
    catalog = retry_call(
        "读取实时 Schema",
        lambda: load_schema_catalog(base_url, manifest["project_id"], headers),
        args.retries,
        args.retry_backoff,
        progress_enabled=progress_enabled,
    )
    progress_log(
        f"OpenSPG={base_url} project_id={manifest['project_id']} "
        f"namespace={manifest['namespace']} mode={args.mode} execute={args.execute} "
        f"wait={args.wait} jobs={len(jobs)} "
        f"shards={'rolling' if args.rolling_shards else ('manifest' if args.shard_manifest else 'off')}",
        progress_enabled=progress_enabled,
    )

    report: list[dict[str, Any]] = []
    templates: dict[int, dict[str, Any]] = {}
    with create_progress(
        enabled=progress_enabled,
        total=len(jobs),
        desc="全量导入任务",
        unit="任务",
        dynamic_ncols=True,
        position=0,
    ) as job_progress:
        for job_index, job in enumerate(jobs, 1):
            source = rolling_source(job) if args.rolling_shards else absolute_path(job["file"])
            if not args.shard_manifest and not source.is_file():
                raise ImportErrorWithContext(f"CSV 文件不存在: {source}")
            transform_label = (
                f" transform={job['rolling_transform']}"
                if args.rolling_shards and job.get("rolling_transform")
                else ""
            )
            job_progress.set_postfix_str(f"当前={job['key']}")
            progress_log(
                f"[{job_index}/{len(jobs)}] {job['key']} <- {source}{transform_label}",
                progress_enabled=progress_enabled,
            )
            if args.shard_manifest:
                units = prebuilt_units(job, shard_index)
                parts = shard_index.get(job["key"])
                if parts is None:
                    raise ImportErrorWithContext(f"分片 manifest 不含任务: {job['key']}")
                unit_total: int | None = len(parts)
            elif args.rolling_shards:
                units = rolling_units(job, args)
                unit_total = None
            else:
                if source.stat().st_size > 200 * 1024 * 1024:
                    raise ImportErrorWithContext(
                        f"CSV 超过 200 MiB，请使用 --rolling-shards 或 --shard-manifest: {source}"
                    )
                units = single_unit(job)
                unit_total = 1

            schema_target = resolve_schema_target(job, manifest, catalog)
            validate_schema_mapping(job, schema_target)
            template = None
            if job.get("template_job_id"):
                template_id = int(job["template_job_id"])
                if template_id not in templates:
                    templates[template_id] = retry_call(
                        "读取模板",
                        lambda tid=template_id: get_template(base_url, tid, headers),
                        args.retries,
                        args.retry_backoff,
                        progress_enabled=progress_enabled,
                    )
                template = templates[template_id]
            job_report = {"key": job["key"], "group": job.get("group"), "parts": []}

            with create_progress(
                enabled=progress_enabled,
                total=unit_total,
                desc=f"{job['key']} 分片",
                unit="片",
                dynamic_ncols=True,
                leave=False,
                position=1,
            ) as shard_progress:
                for unit in units:
                    path: Path = unit["path"]
                    if not path.is_file():
                        raise ImportErrorWithContext(f"分片不存在: {path}")
                    columns = read_csv_header(path)
                    validate_job(job, columns)
                    calculated_digest = sha256sum(
                        path,
                        progress_enabled=progress_enabled,
                        progress_position=2,
                    )
                    digest = unit["sha256"] or calculated_digest
                    if unit["sha256"] and calculated_digest != digest:
                        raise ImportErrorWithContext(f"分片 SHA256 不匹配: {path}")
                    part_no = int(unit["part_no"])
                    state_key = f"{job['key']}:{part_no:05d}:{digest}"
                    prior = state["parts"].get(state_key, {})
                    shard_progress.set_postfix_str(
                        f"part={part_no:05d} size={path.stat().st_size / (1024 * 1024):.1f}MiB"
                    )
                    progress_log(
                        f"  part={part_no:05d} "
                        f"rows={unit['row_count'] if unit['row_count'] is not None else '?'} "
                        f"bytes={path.stat().st_size:,} sha256={digest[:12]}...",
                        progress_enabled=progress_enabled,
                    )
                    if not args.no_resume and prior.get("status") in {"SUBMITTED", "SUCCESS"}:
                        prior_status = prior["status"]
                        if prior_status == "SUBMITTED" and args.wait and prior.get("job_id"):
                            try:
                                builder_status = wait_for_job(
                                    base_url,
                                    int(prior["job_id"]),
                                    headers,
                                    args.poll_interval,
                                    args.timeout,
                                    progress_enabled=progress_enabled,
                                    progress_position=2,
                                )
                            except ImportErrorWithContext:
                                progress_log(
                                    "    断点任务未成功，将重新上传并提交",
                                    progress_enabled=progress_enabled,
                                )
                            else:
                                prior_status = "SUCCESS"
                                prior.update(
                                    {
                                        "status": "SUCCESS",
                                        "builder_status": builder_status,
                                        "updated_at": now_iso(),
                                    }
                                )
                                save_state(state_path, state)
                        accepted_statuses = {"SUCCESS"} if args.wait else {"SUBMITTED", "SUCCESS"}
                        if prior_status in accepted_statuses:
                            progress_log(
                                f"    断点跳过：{prior_status}",
                                progress_enabled=progress_enabled,
                            )
                            if unit["temporary"] or (
                                args.delete_successful_shards and prior_status == "SUCCESS"
                            ):
                                path.unlink(missing_ok=True)
                            job_report["parts"].append(
                                {
                                    **unit,
                                    "path": str(path),
                                    "status": "SKIPPED",
                                    "state_key": state_key,
                                }
                            )
                            shard_progress.update(1)
                            continue

                    part_suffix = f"{args.name_suffix}-part-{part_no:05d}"
                    file_url = "UPLOAD_URL_AFTER_EXECUTION"
                    if args.execute:
                        state["parts"][state_key] = {
                            "job_key": job["key"],
                            "part_no": part_no,
                            "sha256": digest,
                            "file": str(path),
                            "bytes": path.stat().st_size,
                            "row_count": unit["row_count"],
                            "status": "UPLOADING",
                            "updated_at": now_iso(),
                        }
                        save_state(state_path, state)
                        file_url = retry_call(
                            "上传",
                            lambda p=path: upload_file(
                                base_url,
                                p,
                                headers=headers,
                                timeout=args.upload_timeout,
                                chunk_size=max(
                                    MIN_UPLOAD_CHUNK_SIZE,
                                    int(args.upload_chunk_mib * 1024 * 1024),
                                ),
                                progress_enabled=progress_enabled,
                                progress_position=2,
                            ),
                            args.retries,
                            args.retry_backoff,
                            progress_enabled=progress_enabled,
                        )
                        state["parts"][state_key].update(
                            {"status": "UPLOADED", "file_url": file_url, "updated_at": now_iso()}
                        )
                        save_state(state_path, state)
                        progress_log(
                            f"    上传完成: {file_url}",
                            progress_enabled=progress_enabled,
                        )

                    payload = build_payload(
                        manifest,
                        job,
                        template,
                        schema_target,
                        columns,
                        file_url,
                        mode=args.mode,
                        name_suffix=part_suffix,
                        file_name=path.name,
                    )
                    preview_key = f"{job['key']}.part-{part_no:05d}"
                    preview_path = write_preview(args.preview_dir, preview_key, payload)
                    item: dict[str, Any] = {
                        "key": job["key"],
                        "part_no": part_no,
                        "file": str(path),
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                        "rows": unit["row_count"],
                        "payload": str(preview_path),
                        "submitted": False,
                        "status": "DRY_RUN",
                        "state_key": state_key,
                    }
                    if args.execute:
                        result = retry_call(
                            "提交",
                            lambda: json_request(
                                base_url,
                                "/public/v1/builder/job/submit",
                                method="POST",
                                payload=payload,
                                headers=headers,
                                timeout=120,
                            ).get("result"),
                            args.retries,
                            args.retry_backoff,
                            progress_enabled=progress_enabled,
                        )
                        job_id = result.get("id") if isinstance(result, dict) else None
                        if not job_id and args.mode == "update":
                            job_id = job["template_job_id"]
                        item.update(
                            {
                                "submitted": True,
                                "job_id": job_id,
                                "file_url": file_url,
                                "status": "SUBMITTED",
                            }
                        )
                        state["parts"][state_key].update(
                            {
                                "status": "SUBMITTED",
                                "job_id": job_id,
                                "file_url": file_url,
                                "updated_at": now_iso(),
                            }
                        )
                        save_state(state_path, state)
                        progress_log(
                            f"    已提交 Builder Job: {job_id or result}",
                            progress_enabled=progress_enabled,
                        )
                        if args.wait:
                            if not job_id:
                                raise ImportErrorWithContext(f"提交结果没有 job_id: {result}")
                            status = retry_call(
                                "等待任务",
                                lambda jid=int(job_id): wait_for_job(
                                    base_url,
                                    jid,
                                    headers,
                                    args.poll_interval,
                                    args.timeout,
                                    progress_enabled=progress_enabled,
                                    progress_position=2,
                                ),
                                args.retries,
                                args.retry_backoff,
                                progress_enabled=progress_enabled,
                            )
                            item["status"] = status
                            state["parts"][state_key].update(
                                {
                                    "status": "SUCCESS",
                                    "builder_status": status,
                                    "updated_at": now_iso(),
                                }
                            )
                            save_state(state_path, state)
                            if unit["temporary"] or args.delete_successful_shards:
                                path.unlink(missing_ok=True)
                                item["local_deleted"] = True
                                progress_log(
                                    "    已确认成功并删除本地分片",
                                    progress_enabled=progress_enabled,
                                )
                    job_report["parts"].append(item)
                    if unit["temporary"] and not args.execute:
                        path.unlink(missing_ok=True)
                        item["local_deleted"] = True
                    shard_progress.update(1)
            report.append(job_report)
            job_progress.update(1)

    args.preview_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.preview_dir / "import-report.json"
    report_path.write_text(
        json.dumps(
            {
                "created_at": now_iso(),
                "execute": args.execute,
                "mode": args.mode,
                "base_url": base_url,
                "project_id": manifest["project_id"],
                "namespace": manifest["namespace"],
                "state_file": str(state_path),
                "jobs": report,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    progress_log(f"完成。报告: {report_path}", progress_enabled=progress_enabled)
    if not args.execute:
        progress_log(
            "当前为 dry-run，未上传文件、未提交 Builder Job。",
            progress_enabled=progress_enabled,
        )
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImportErrorWithContext, OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
