#!/usr/bin/env python3
"""OpenSPG REST 客户端：登录、项目、Schema、上传、Builder Job。

登录协议（从 release-openspg-server 反编译 AccountServicePublicImpl 确认）：
- POST /v1/accounts/login，JSON 体 {account, password}
- 与 OpenSPG UI 完全一致：password 字段发送 sha256Hex(明文密码 + "OPENSPG")
  （前端 pwdCryptoSha256，见登录 chunk fcaf5dab）；
  服务端校验 sha256Hex(收到的password + kg_user.salt) == kg_user.dw_access_key
- 成功后 Set-Cookie OPEN_SPG_TOKEN=<AES-CTR(account:password, open_spg_token_secret)>
- /public/** 免认证；/v1/** 需要 OPEN_SPG_TOKEN Cookie

本机 kg_user.openspg 的 dw_access_key 已按该约定重置（明文密码
openspg@kag2026，盐 Ktu4O），UI 与本客户端均可登录。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8887"
DEFAULT_ACCOUNT = "openspg"
DEFAULT_PASSWORD = "openspg@kag2026"
TOKEN_COOKIE = "OPEN_SPG_TOKEN"
TOKEN_TTL_SECONDS = 12 * 3600  # cookie maxAge=43200，保守提前 1 小时续期

TERMINAL_SUCCESS = {"FINISH", "SUCCESS", "SUCCEEDED"}
TERMINAL_FAILURE = {
    "FAIL", "FAILED", "ERROR", "CANCELED", "CANCELLED",
    "TERMINATE", "TERMINATED", "STOP", "STOPPED", "ABORTED",
}


class SpgClientError(RuntimeError):
    pass


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SpgClient:
    """带自动登录与 token 续期的 OpenSPG REST 封装。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        account: str = DEFAULT_ACCOUNT,
        password: str = DEFAULT_PASSWORD,
        timeout: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.account = account
        self.password = password
        self.timeout = timeout
        self._token: str | None = None
        self._token_ts: float = 0.0

    # ------------------------------------------------------------------ auth

    def login(self) -> str:
        """登录并把 OPEN_SPG_TOKEN 缓存在内存。"""
        raw = self._raw_request(
            "/v1/accounts/login",
            method="POST",
            # 与 OpenSPG UI 完全一致（前端 pwdCryptoSha256，见 fcaf5dab chunk）：
            # password 字段发送 sha256Hex(明文 + "OPENSPG")；服务端再加盐比对：
            # dw_access_key = sha256Hex(sha256Hex(明文 + "OPENSPG") + salt)
            payload={"account": self.account, "password": sha256_hex(self.password + "OPENSPG")},
            headers={},
            with_token=False,
            return_headers=True,
        )
        body, info = raw
        data = json.loads(body or "{}")
        if not data.get("success"):
            raise SpgClientError(f"登录失败: {data}")
        cookie = info.get("Set-Cookie") or ""
        token = ""
        for part in cookie.split(";"):
            if part.strip().startswith(f"{TOKEN_COOKIE}="):
                token = part.split("=", 1)[1].strip()
        if not token:
            raise SpgClientError(f"登录成功但没有 {TOKEN_COOKIE} cookie: {cookie!r}")
        self._token = token
        self._token_ts = time.time()
        return token

    def token(self) -> str:
        if self._token and time.time() - self._token_ts < TOKEN_TTL_SECONDS:
            return self._token
        return self.login()

    def auth_headers(self, *, with_token: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if with_token:
            headers["Cookie"] = f"{TOKEN_COOKIE}={self.token()}"
        return headers

    # --------------------------------------------------------------- requests

    def _raw_request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | Iterator[bytes] | None = None,
        timeout: int | None = None,
        with_token: bool = True,
        return_headers: bool = False,
    ) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request_headers = self.auth_headers(with_token=with_token)
        request_headers.update(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                info = dict(resp.headers)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SpgClientError(f"HTTP {exc.code} {url}: {detail[:2000]}") from exc
        except URLError as exc:
            raise SpgClientError(f"请求失败 {url}: {exc}") from exc
        if return_headers:
            return raw, info
        return raw

    def json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        with_token: bool = True,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        raw = self._raw_request(
            path,
            method=method,
            params=params,
            payload=payload,
            with_token=with_token,
            timeout=timeout,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpgClientError(f"接口未返回 JSON {path}: {raw[:500]}") from exc
        # queryProjectSchema 返回裸对象；其余多数返回 success 包装
        if isinstance(data, dict) and data.get("success") is False:
            raise SpgClientError(f"接口执行失败 {path}: {data.get('errorMsg') or data}")
        return data

    def retry(self, label: str, fn, retries: int = 3, backoff: float = 2.0):
        last: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                return fn()
            except (SpgClientError, OSError) as exc:
                last = exc
                if attempt > retries:
                    raise
                delay = backoff * (2 ** (attempt - 1))
                print(f"    {label}失败（{attempt}/{retries + 1}）：{exc}；{delay:.1f}s 后重试", flush=True)
                time.sleep(delay)
        assert last is not None
        raise last

    # ---------------------------------------------------------------- upload

    def upload_file(self, path: Path, *, timeout: int = 1800, chunk_size: int = 1024 * 1024) -> str:
        """流式 multipart 上传，返回 MinIO fileUrl。"""
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

        def body_iter() -> Iterator[bytes]:
            yield prefix
            with path.open("rb") as fh:
                while True:
                    block = fh.read(chunk_size)
                    if not block:
                        break
                    yield block
            yield suffix

        raw = self._raw_request(
            "/public/v1/reasoner/dialog/uploadFile",
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(content_length),
            },
            body=body_iter(),
            timeout=timeout,
            with_token=False,  # public 接口；带 token 在部分版本会被拒
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpgClientError(f"上传接口未返回 JSON: {raw[:500]}") from exc
        if not data.get("success") or not data.get("result"):
            raise SpgClientError(f"上传失败 {path}: {data}")
        return str(data["result"])

    # --------------------------------------------------------------- project

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        try:
            data = self.json_request(f"/v1/projects/{int(project_id)}")
        except SpgClientError:
            return None
        return data.get("result") if data.get("success") else None

    def find_project_by_namespace(self, namespace: str) -> dict[str, Any] | None:
        """查项目列表定位 namespace。"""
        data = self.json_request("/v1/projects/list", params={"pageSize": 100, "pageNum": 1})
        result = data.get("result")
        if isinstance(result, dict):
            for item in result.get("records") or result.get("list") or []:
                if isinstance(item, dict) and item.get("namespace") == namespace:
                    return item
        return None

    def create_project(self, payload: dict[str, Any]) -> int:
        data = self.json_request("/v1/projects", method="POST", payload=payload)
        result = data.get("result")
        if not isinstance(result, int):
            raise SpgClientError(f"创建项目返回异常: {data}")
        return result

    # ---------------------------------------------------------------- schema

    def query_project_schema(self, project_id: int) -> dict[str, Any]:
        return self.json_request(
            "/public/v1/schema/queryProjectSchema",
            params={"projectId": int(project_id)},
            with_token=False,
            timeout=120,
        )

    def save_schema(self, schema_text: str) -> dict[str, Any]:
        return self.json_request(
            "/v1/schemas",
            method="POST",
            payload={"data": schema_text},
            timeout=300,
        )

    def get_schema_script(self, project_id: int) -> str:
        data = self.json_request(f"/v1/schemas/{int(project_id)}/script")
        return str(data.get("result") or "")

    # ----------------------------------------------------------- builder job

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.json_request(
            "/public/v1/builder/job/submit",
            method="POST",
            payload=payload,
            timeout=180,
        )
        result = data.get("result")
        if not isinstance(result, dict) or not result.get("id"):
            raise SpgClientError(f"提交 Builder Job 失败: {data}")
        return result

    def get_job(self, job_id: int) -> dict[str, Any]:
        data = self.json_request(
            "/public/v1/builder/job/get", params={"id": int(job_id)}, with_token=False
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise SpgClientError(f"任务不存在: {job_id}")
        return result

    def wait_for_job(
        self, job_id: int, *, interval: int = 10, timeout: int = 7200, log=print
    ) -> str:
        started = time.time()
        previous: str | None = None
        poll_errors = 0
        while time.time() - started < timeout:
            try:
                job = self.get_job(job_id)
                poll_errors = 0
            except (SpgClientError, OSError) as exc:
                # 轮询瞬断（服务端高负载 60s 无响应）不视为任务失败，继续轮询
                poll_errors += 1
                if poll_errors % 6 == 1:
                    log(f"    轮询 Builder Job {job_id} 瞬时失败（连续 {poll_errors} 次）：{exc}")
                if poll_errors > 60:
                    raise
                time.sleep(interval)
                continue
            status = str(job.get("status") or "UNKNOWN").upper()
            if status != previous:
                log(f"    Builder Job {job_id} 状态: {status} (elapsed {int(time.time()-started)}s)")
                previous = status
            if status in TERMINAL_SUCCESS:
                return status
            if status in TERMINAL_FAILURE:
                raise SpgClientError(f"Builder Job {job_id} 失败: {status}; progress={job.get('progress')}")
            time.sleep(interval)
        raise SpgClientError(f"等待 Builder Job {job_id} 超时（{timeout}s）")


def load_schema_catalog(project_schema: dict[str, Any]) -> dict[str, Any]:
    """把 queryProjectSchema 结果转成 {entities, relations} 目录。

    沿用 scripts/import_openspg_csvs.py 的解析口径（只读复用逻辑）。
    """
    def type_name(value: dict[str, Any] | None) -> str:
        if not isinstance(value, dict):
            return ""
        name = (value.get("basicInfo") or {}).get("name") or {}
        return str(name.get("nameEn") or name.get("name") or "")

    def qualified(value: dict[str, Any] | None) -> str:
        if not isinstance(value, dict):
            return ""
        name = (value.get("basicInfo") or {}).get("name") or {}
        ns = str(name.get("namespace") or "")
        en = type_name(value)
        return f"{ns}.{en}" if ns and en else en

    types = project_schema.get("spgTypes")
    if not isinstance(types, list):
        raise SpgClientError("queryProjectSchema 未返回 spgTypes")

    entities: dict[str, dict[str, Any]] = {}
    relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in types:
        if not isinstance(item, dict) or item.get("spgTypeEnum") != "ENTITY_TYPE":
            continue
        basic = item.get("basicInfo") or {}
        name_obj = basic.get("name") or {}
        ns = str(name_obj.get("namespace") or "")
        short = type_name(item)
        if not (ns and short):
            continue
        entity = {
            "name": short,
            "qualified_name": f"{ns}.{short}",
            "name_zh": str(basic.get("nameZh") or short),
            "id": (item.get("ontologyId") or {}).get("uniqueId"),
            "properties": set(),
        }
        for prop in item.get("properties") or []:
            prop_name = str((prop.get("basicInfo") or {}).get("name", {}).get("name") or "")
            if prop_name:
                entity["properties"].add(prop_name)
        entities[short] = entity
        for relation in item.get("relations") or []:
            rel_name = str((relation.get("basicInfo") or {}).get("name", {}).get("name") or "")
            target = qualified(relation.get("objectTypeRef"))
            if not (rel_name and target):
                continue
            relations[(short, rel_name, target.rsplit(".", 1)[-1])] = {
                "name": rel_name,
                "name_zh": str((relation.get("basicInfo") or {}).get("nameZh") or rel_name),
                "id": (relation.get("ontologyId") or {}).get("uniqueId"),
                "source_type": f"{ns}.{short}",
                "target_type": target,
                "properties": {
                    str((prop.get("basicInfo") or {}).get("name", {}).get("name"))
                    for prop in (relation.get("advancedConfig") or {}).get("subProperties") or []
                    if (prop.get("basicInfo") or {}).get("name", {}).get("name")
                },
            }
    if not entities:
        raise SpgClientError("项目没有实体 Schema")
    return {"entities": entities, "relations": relations}


def resolve_schema_target(job: dict[str, Any], namespace: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """把 manifest 的 schema_target 解析为实时 ontology 元数据。"""
    target = job.get("schema_target") or {}
    kind = target.get("kind")
    if kind == "entity":
        type_name = str(target.get("type") or "")
        entity = catalog["entities"].get(type_name)
        if not entity:
            raise SpgClientError(f"{job['key']} 对应实体不存在: {type_name}")
        return {"kind": kind, **entity}
    if kind == "relation":
        source_short = str(target.get("source_type") or "").rsplit(".", 1)[-1]
        rel = str(target.get("relation") or "")
        target_short = str(target.get("target_type") or "").rsplit(".", 1)[-1]
        relation = catalog["relations"].get((source_short, rel, target_short))
        if not relation:
            raise SpgClientError(
                f"{job['key']} 对应关系不存在: {namespace}.{source_short}.{rel}->{target_short}"
            )
        return {"kind": kind, **relation}
    raise SpgClientError(f"{job['key']} 缺少有效 schema_target.kind")
