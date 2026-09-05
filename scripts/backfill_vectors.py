#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bge-m3 向量回填脚本（zwdmxgj 图，修复导入期 mock 零向量）。

背景
----
zwdmxgj 库各实体的向量属性（`_name_vector` / `_content_vector` / `_answer_vector`）
是导入期由 mock 嵌入服务写入的 1024 维全零向量；向量索引（COSINE，1024 维）已由
OpenSPG 建好，回填只需 SET 节点属性，索引自动更新。

嵌入源：本机 ollama `POST {OLLAMA_URL}/api/embeddings`，
body {"model": "bge-m3", "prompt": <text>} → {"embedding": [1024 floats]}。
启动时不做连通性探测（模型可能仍在下载）；首次实际调用失败会给出清晰错误并退出。

回填目标（--targets 逗号分隔，默认全部；量级为实测）
----------------------------------------------------
    key                标签                       文本属性    向量属性            量级
    service            ZwdmxGJ.GovernmentService  name        _name_vector        ~48 万
    material           ZwdmxGJ.Material           name        _name_vector        ~3 万
    citation_name      ZwdmxGJ.LegalCitation      name        _name_vector        ~9.4k
    citation_content   ZwdmxGJ.LegalCitation      content     _content_vector     ~9.4k
    basis              ZwdmxGJ.LegalBasis         name        _name_vector        ~2.8k
    condition          ZwdmxGJ.ServiceCondition   name        _name_vector        ~48 万（name 即受理条件长文本）
    faq_name           ZwdmxGJ.FAQ                name        _name_vector        ~12.6 万
    faq_answer         ZwdmxGJ.FAQ                answer      _answer_vector      ~12.6 万
    chunk              ZwdmxGJ.Chunk              content     _content_vector     当前 0（未导入，支持但空跑）
不含 ProcessStep（~155 万且检索面未用其向量）。statement / checkStandard 文本属性
当前普遍为空、对应向量属性不存在，按"文本为空则跳过该属性"处理，故不列 target。

向量属性名规律：文本属性 X → `_<snake(X)>_vector`（name→_name_vector、
content→_content_vector、answer→_answer_vector、checkStandard→_check_standard_vector）。

--service-ids 子集模式
----------------------
每行一个 serviceId 的文件（或 "-" 读 stdin），只回填这些事项与其关联实体：
    事项自身（serviceId IN）
    -[:requiresMaterial]->(Material)
    -[:citesLegal]->(LegalCitation)            → citation_name / citation_content
    -[:citesLegal]->(:LegalCitation)-[:partOf]->(LegalBasis)   （2 跳）→ basis
    -[:hasCondition]->(ServiceCondition)
    -[:hasFaq]->(FAQ)
    -[:hasChunk]->(Chunk)                       → chunk（hasChunk 未列入任务清单，
                                                  但 chunk target 存在，一并支持；当前 0 节点）
请求的每个 target 分别按上表扩展后、按其 id 属性回填。

断点续跑（--state-file，默认 kg/import/checkpoints/backfill_state.json）
----------------------------------------------------------------------
JSON Lines：每行 {"target": <key>, "id": <节点id>}。每批 SET 成功后追加并
flush，Ctrl-C / 异常后重跑同一命令自动跳过已完成节点（幂等：默认也跳过向量
已非零的节点）。dry-run 不写 state；全部完成后可归档或清空该文件。

用法示例
--------
    # 全部目标 dry-run（不调 ollama、不写图、不写 state）
    python scripts/backfill_vectors.py --dry-run
    # 小目标先验证链路（LegalBasis ~2.8k）
    python scripts/backfill_vectors.py --targets basis
    # 按事项子集回填该事项及其关联实体的全部向量
    python scripts/backfill_vectors.py --service-ids data/pilot/service_ids.txt
    # 冒烟：service 目标前 8 个节点
    python scripts/backfill_vectors.py --targets service --limit 8

环境变量：NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE（默认
bolt://127.0.0.1:7687 / neo4j / neo4j@openspg / zwdmxgj）、
OLLAMA_URL（默认 http://127.0.0.1:11434）、BGE_MODEL（默认 bge-m3）。

已知风险（使用说明）
--------------------
  * ollama /api/embeddings 是单条语义，无并发：全量 service（~48 万）吞吐由单条
    嵌入延迟决定（CPU 上单条可达秒级），请优先用 --service-ids 子集或分批跑。
  * 大批量 SET 会触发向量索引重写（写放大）；--batch-size 过大会放大单事务。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from neo4j import GraphDatabase

try:  # 测试桩环境下 neo4j.exceptions 可能不存在，见 tests/test_backfill_vectors.py
    from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable

    _NEO4J_FATAL = (Neo4jError, ServiceUnavailable, AuthError)
except ImportError:  # pragma: no cover
    _NEO4J_FATAL = ()

# ---------------------------------------------------------------- 配置

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_FILE = REPO_ROOT / "kg" / "import" / "checkpoints" / "backfill_state.json"

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j@openspg")
NEO4J_DB = os.environ.get("NEO4J_DATABASE", "zwdmxgj")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("BGE_MODEL", "bge-m3")
EMBED_DIM = 1024
EMBED_TIMEOUT = 60.0

# 连续嵌入失败达到该阈值判定 ollama 不可用（过载/模型异常），中止整个回填
MAX_CONSECUTIVE_FAILURES = 16

# ---------------------------------------------------------------- 回填目标

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_prop(name: str) -> str:
    """camelCase → snake_case（checkStandard → check_standard）。"""
    return _CAMEL_RE.sub("_", name).lower()


def vector_prop_for(text_prop: str) -> str:
    """文本属性 → 向量属性名：X → `_<snake(X)>_vector`。"""
    return f"_{snake_prop(text_prop)}_vector"


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    text_prop: str
    id_prop: str

    @property
    def vector_prop(self) -> str:
        return vector_prop_for(self.text_prop)


# 顺序即默认执行顺序（小目标建议先跑：basis/citation 验证链路后再跑大目标）
TARGETS: dict[str, Target] = {
    "service": Target("service", "ZwdmxGJ.GovernmentService", "name", "serviceId"),
    "material": Target("material", "ZwdmxGJ.Material", "name", "materialId"),
    "citation_name": Target("citation_name", "ZwdmxGJ.LegalCitation", "name", "citationId"),
    "citation_content": Target("citation_content", "ZwdmxGJ.LegalCitation", "content", "citationId"),
    "basis": Target("basis", "ZwdmxGJ.LegalBasis", "name", "legalBasisId"),
    "condition": Target("condition", "ZwdmxGJ.ServiceCondition", "name", "conditionId"),
    "faq_name": Target("faq_name", "ZwdmxGJ.FAQ", "name", "faqId"),
    "faq_answer": Target("faq_answer", "ZwdmxGJ.FAQ", "answer", "faqId"),
    "chunk": Target("chunk", "ZwdmxGJ.Chunk", "content", "chunkId"),
}

SERVICE_LABEL = "ZwdmxGJ.GovernmentService"
SERVICE_ID_PROP = "serviceId"

# --service-ids 子集模式下各 target 的关联扩展路径（关系, 标签）序列；
# () = 事项自身按 serviceId IN 直查。标签带命名空间前缀且含点，Cypher 中必须反引号。
SERVICE_EXPANSION: dict[str, tuple[tuple[str, str], ...]] = {
    "service": (),
    "material": (("requiresMaterial", "ZwdmxGJ.Material"),),
    "citation_name": (("citesLegal", "ZwdmxGJ.LegalCitation"),),
    "citation_content": (("citesLegal", "ZwdmxGJ.LegalCitation"),),
    "basis": (("citesLegal", "ZwdmxGJ.LegalCitation"), ("partOf", "ZwdmxGJ.LegalBasis")),
    "condition": (("hasCondition", "ZwdmxGJ.ServiceCondition"),),
    "faq_name": (("hasFaq", "ZwdmxGJ.FAQ"),),
    "faq_answer": (("hasFaq", "ZwdmxGJ.FAQ"),),
    "chunk": (("hasChunk", "ZwdmxGJ.Chunk"),),
}

# ---------------------------------------------------------------- Cypher 构建


def _hop_path(hops: Sequence[tuple[str, str]], bind_last: str) -> str:
    """(关系, 标签) 序列 → Cypher 路径片段；最后一跳绑定节点变量 bind_last。"""
    parts = [f"-[:`{rel}`]->(:`{label}`)" for rel, label in hops[:-1]]
    last_rel, last_label = hops[-1]
    parts.append(f"-[:`{last_rel}`]->({bind_last}:`{last_label}`)")
    return "".join(parts)


def read_query(target: Target, subset: bool) -> str:
    """读节点分页 Cypher（keyset 分页：WHERE id > $after ORDER BY id LIMIT $page）。

    subset=False 全量扫描；True 按 --service-ids 子集（service 自身 id IN，
    其余 target 按 SERVICE_EXPANSION 关系路径扩展，DISTINCT 去共享实体重）。
    """
    t = target
    hops = SERVICE_EXPANSION.get(t.key, ())
    if subset and hops:
        return (
            f"MATCH (s:`{SERVICE_LABEL}`) WHERE s.`{SERVICE_ID_PROP}` IN $ids "
            f"MATCH (s){_hop_path(hops, 'n')} "
            f"WHERE n.`{t.id_prop}` > $after "
            f"RETURN DISTINCT n.`{t.id_prop}` AS id, n.`{t.text_prop}` AS text, "
            f"n.`{t.vector_prop}` AS vec ORDER BY id LIMIT $page"
        )
    where = f"n.`{t.id_prop}` > $after"
    if subset:
        where += f" AND n.`{t.id_prop}` IN $ids"
    return (
        f"MATCH (n:`{t.label}`) WHERE {where} "
        f"RETURN n.`{t.id_prop}` AS id, n.`{t.text_prop}` AS text, "
        f"n.`{t.vector_prop}` AS vec ORDER BY id LIMIT $page"
    )


def count_query(target: Target, subset: bool) -> str:
    """候选节点总数（含已回填/空文本节点，仅用于进度展示与零节点早退）。"""
    t = target
    hops = SERVICE_EXPANSION.get(t.key, ())
    if subset and hops:
        return (
            f"MATCH (s:`{SERVICE_LABEL}`) WHERE s.`{SERVICE_ID_PROP}` IN $ids "
            f"MATCH (s){_hop_path(hops, 'n')} "
            f"RETURN count(DISTINCT n) AS c"
        )
    where = f"n.`{t.id_prop}` IN $ids" if subset else ""
    if where:
        return f"MATCH (n:`{t.label}`) WHERE {where} RETURN count(n) AS c"
    return f"MATCH (n:`{t.label}`) RETURN count(n) AS c"


def write_query(target: Target) -> str:
    """批量写回：UNWIND 行按 id 定位节点，SET 向量属性（向量索引自动更新）。"""
    t = target
    return (
        f"UNWIND $rows AS row "
        f"MATCH (n:`{t.label}`) WHERE n.`{t.id_prop}` = row.id "
        f"SET n.`{t.vector_prop}` = row.vec"
    )


# ---------------------------------------------------------------- 工具函数


def is_missing_or_zero(vec) -> bool:
    """向量缺失 / 非法 / 全零 → True（需要回填）。空列表视为缺失。"""
    if vec is None or not isinstance(vec, (list, tuple)):
        return True
    try:
        return all(float(v) == 0.0 for v in vec)
    except (TypeError, ValueError):
        return True


def chunked(items: Sequence, size: int) -> Iterator[list]:
    """按 size 切分（不足一批的尾部单独成批）。"""
    if size < 1:
        raise ValueError("chunk size 必须 >= 1")
    materialized = list(items)
    for i in range(0, len(materialized), size):
        yield materialized[i:i + size]


def read_service_ids(path: str) -> list[str]:
    """读取 serviceId 清单：每行一个，忽略空行与 # 注释，去重保序。

    path 为 "-" 时读 stdin；容忍 utf-8-sig BOM；空清单抛 ValueError。
    """
    if path == "-":
        lines = sys.stdin.read().splitlines()
    else:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"service-ids 文件不存在：{p}")
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    ids: list[str] = []
    seen: set[str] = set()
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s not in seen:
            seen.add(s)
            ids.append(s)
    if not ids:
        raise ValueError(f"service-ids 输入为空：{path}")
    return ids


def _default_log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 断点 state


class BackfillState:
    """已完成 (target, id) 集合，JSON Lines 追加持久化（每批写后 flush）。

    追加式而非整文件重写，避免大进度下每批 O(n) 重写；
    末行写半截（进程被杀）在加载时按坏行忽略并告警。read_only 模式（dry-run）
    只加载、不追加，不创建文件。
    """

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._done: dict[str, set[str]] = {}
        self._fh = None
        bad = 0
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self._done.setdefault(str(rec["target"]), set()).add(str(rec["id"]))
                    except (ValueError, KeyError, TypeError):
                        bad += 1
        if bad:
            print(f"[警告] state 文件 {self.path} 有 {bad} 行无法解析（忽略，按未完成处理）",
                  file=sys.stderr)

    def contains(self, target: str, node_id: str) -> bool:
        bucket = self._done.get(target)
        return bucket is not None and node_id in bucket

    def mark_many(self, target: str, node_ids: Sequence[str]) -> None:
        if self.read_only or not node_ids:
            return
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        bucket = self._done.setdefault(target, set())
        for node_id in node_ids:
            if node_id in bucket:  # 幂等，不重复追加
                continue
            self._fh.write(json.dumps({"target": target, "id": node_id},
                                      ensure_ascii=False) + "\n")
            bucket.add(node_id)
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- ollama 嵌入


class EmbedServiceError(RuntimeError):
    """ollama 服务级错误（连不上 / HTTP 错 / 模型缺失 / 响应非法 / 维度不符），中止回填。"""


class EmbedTimeoutError(Exception):
    """单条文本嵌入超时：该条计为 failed，后续文本继续。"""


class OllamaEmbedder:
    """顺序嵌入器：POST {base_url}/api/embeddings，单条单请求（ollama 单条语义）。"""

    def __init__(self, base_url: str = OLLAMA_URL, model: str = EMBED_MODEL,
                 timeout: float = EMBED_TIMEOUT, expected_dim: int = EMBED_DIM,
                 max_chars: int = 0, batch_api: bool = False):
        self.endpoint = base_url.rstrip("/") + "/api/embeddings"
        self.model = model
        self.timeout = timeout
        self.expected_dim = expected_dim
        # 嵌入前截断上限（字符数）；0=不截断。bge-m3 上下文 8192 token，
        # 长法条/长条件全文会超限，检索用嵌入取前缀即可。
        self.max_chars = max_chars
        # True 时 embed_batch 走 ollama 批量端点 /api/embed（input 数组，GPU 并行，
        # 实测约 150 texts/s vs 单条约 6/s）；num_ctx 按单条窗口生效。
        self.batch_api = batch_api

    def embed_one(self, text: str) -> list[float]:
        if self.max_chars > 0 and len(text) > self.max_chars:
            text = text[:self.max_chars]
        body = json.dumps({"model": self.model, "prompt": text,
                           "options": {"num_ctx": 8192}},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            raise EmbedServiceError(
                f"ollama 返回 HTTP {e.code}（{self.endpoint}, model={self.model}）：{detail}"
                f" —— 请确认 ollama serve 在运行且模型已完整拉取（ollama pull {self.model}）") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise EmbedTimeoutError(
                    f"ollama 嵌入连接超时（>{self.timeout}s，{self.endpoint}）") from e
            raise EmbedServiceError(
                f"无法连接 ollama（{self.endpoint}）：{e.reason!r}"
                f" —— 请确认 ollama serve 在运行（默认 http://127.0.0.1:11434）") from e
        except (socket.timeout, TimeoutError) as e:
            raise EmbedTimeoutError(
                f"ollama 嵌入读取超时（>{self.timeout}s，{self.endpoint}）") from e
        except OSError as e:
            raise EmbedServiceError(f"ollama 请求失败（{self.endpoint}）：{e!r}") from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise EmbedServiceError(
                f"ollama 响应不是合法 JSON（{self.endpoint}）：{raw[:200]!r}") from e
        vec = payload.get("embedding") if isinstance(payload, dict) else None
        if (not isinstance(vec, list) or not vec
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in vec)):
            raise EmbedServiceError(
                f"ollama 返回的 embedding 非法（{self.endpoint}, model={self.model}）："
                f"{str(payload)[:200]}")
        if len(vec) != self.expected_dim:
            raise EmbedServiceError(
                f"向量维度 {len(vec)} != 预期 {self.expected_dim}（model={self.model} 不是 "
                f"{self.expected_dim} 维？请核对 BGE_MODEL / ollama 模型标签）")
        return [float(v) for v in vec]

    def _truncate(self, text: str) -> str:
        if self.max_chars > 0 and len(text) > self.max_chars:
            return text[:self.max_chars]
        return text

    def embed_batch_api(self, texts: Sequence[str]) -> list[list[float] | None]:
        """ollama 批量端点 /api/embed：一次请求嵌入整批。服务级错误整体抛出。"""
        body = json.dumps(
            {"model": self.model, "input": [self._truncate(t) for t in texts],
             "options": {"num_ctx": 8192}},
            ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint.rsplit("/", 2)[0] + "/api/embed", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            raise EmbedServiceError(
                f"ollama 批量嵌入 HTTP {e.code}（model={self.model}）：{detail}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            raise EmbedServiceError(f"ollama 批量嵌入请求失败：{e!r}") from e
        try:
            payload = json.loads(raw.decode("utf-8"))
            embs = payload.get("embeddings") or []
        except (UnicodeDecodeError, ValueError) as e:
            raise EmbedServiceError(f"ollama 批量响应解析失败：{e!r}") from e
        if len(embs) != len(texts):
            raise EmbedServiceError(
                f"ollama 批量返回数不匹配：{len(embs)} != {len(texts)}")
        out: list[list[float] | None] = []
        for v in embs:
            if not isinstance(v, list) or len(v) != self.expected_dim:
                out.append(None)
            else:
                out.append(v)
        return out

    def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        """批量嵌入：batch_api=True 走 /api/embed；否则顺序逐条。单条失败计 None。"""
        if self.batch_api:
            return self.embed_batch_api(texts)
        out: list[list[float] | None] = []
        for text in texts:
            try:
                out.append(self.embed_one(text))
            except EmbedTimeoutError:
                out.append(None)
        return out


# ---------------------------------------------------------------- 主流程

_SUMMARY_COLS = ("scanned", "skipped_empty", "skipped_done", "embedded", "failed")


def process_target(
    target: Target,
    *,
    session,
    embedder,
    state: BackfillState,
    service_ids: Sequence[str] | None = None,
    batch_size: int = 64,
    limit: int | None = None,
    include_nonzero: bool = False,
    dry_run: bool = False,
    page_size: int | None = None,
    log: Callable[[str], None] = _default_log,
) -> dict[str, int]:
    """回填单个 target，返回 {scanned, skipped_empty, skipped_done, embedded, failed}。

    分页读（keyset）→ 过滤（空文本 / state 已完成 / 默认跳过非零向量）
    → 分批嵌入（batch_size）→ UNWIND SET → state 追加。dry-run 不嵌入不写图不写 state。
    """
    if batch_size < 1:
        raise ValueError("batch_size 必须 >= 1")
    if page_size is None:
        page_size = batch_size * 8
    subset = service_ids is not None
    ids = list(service_ids) if service_ids else []
    counters = {c: 0 for c in _SUMMARY_COLS}
    tag = "[dry-run]" if dry_run else ""

    count_rows = list(session.run(count_query(target, subset), ids=ids))
    total = int(count_rows[0]["c"]) if count_rows else 0
    log(f"[{target.key}]{tag} 开始：候选节点 {total} 个"
        + (f"（{len(ids)} 个 serviceId 子集）" if subset else ""))
    if total == 0:
        return counters

    t0 = time.monotonic()
    after = ""
    consecutive_failures = 0

    def progress() -> None:
        elapsed = max(time.monotonic() - t0, 1e-9)
        rate = counters["embedded"] / elapsed
        log(f"[{target.key}]{tag} 进度 {counters['embedded']}/{total}"
            f"（skipped: done={counters['skipped_done']}, empty={counters['skipped_empty']};"
            f" failed={counters['failed']}）{rate:.1f} nodes/s")

    while True:
        rows = list(session.run(read_query(target, subset),
                                ids=ids, after=after, page=page_size))
        if not rows:
            break
        after = rows[-1]["id"]

        pending: list[tuple[str, str]] = []
        for row in rows:
            counters["scanned"] += 1
            rid = row["id"]
            text = row["text"]
            if not isinstance(text, str):
                text = "" if text is None else str(text)
            if not text.strip():
                counters["skipped_empty"] += 1
                continue
            if state.contains(target.key, rid):
                counters["skipped_done"] += 1
                continue
            if not include_nonzero and not is_missing_or_zero(row["vec"]):
                counters["skipped_done"] += 1
                continue
            pending.append((rid, text))

        for batch in chunked(pending, batch_size):
            if limit is not None:
                remaining = limit - counters["embedded"]
                if remaining <= 0:
                    break
                batch = batch[:remaining]
            if dry_run:
                counters["embedded"] += len(batch)
                progress()
                continue
            vectors = embedder.embed_batch([text for _, text in batch])
            to_write = []
            for (rid, _), vec in zip(batch, vectors):
                if vec is None:
                    counters["failed"] += 1
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    to_write.append({"id": rid, "vec": vec})
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"[{target.key}] 连续 {consecutive_failures} 条文本嵌入失败"
                    f"（ollama 过载或模型异常？），中止回填。已完成 {counters['embedded']} 个节点的"
                    f"进度已写入 state（{state.path}），重跑同一命令可续作。")
            if to_write:
                session.run(write_query(target), rows=to_write).consume()
                state.mark_many(target.key, [r["id"] for r in to_write])
                counters["embedded"] += len(to_write)
            progress()

        if len(rows) < page_size:
            break
        if limit is not None and counters["embedded"] >= limit:
            break

    log(f"[{target.key}]{tag} 完成：scanned={counters['scanned']} "
        f"embedded={counters['embedded']} "
        f"skipped(done={counters['skipped_done']}, empty={counters['skipped_empty']}) "
        f"failed={counters['failed']} 用时 {time.monotonic() - t0:.1f}s")
    return counters


def print_summary(results: dict[str, dict[str, int]], dry_run: bool = False,
                  log: Callable[[str], None] = _default_log) -> None:
    embed_col = "to_embed" if dry_run else "embedded"
    log("")
    suffix = "（dry-run：未调 ollama、未写图、未写 state）" if dry_run else ""
    log(f"===== 回填汇总{suffix} =====")
    log(f"{'target':<18}{'scanned':>10}{'skipped_empty':>15}"
        f"{'skipped_done':>14}{embed_col:>10}{'failed':>8}")
    totals = {c: 0 for c in _SUMMARY_COLS}
    for key, c in results.items():
        log(f"{key:<18}{c['scanned']:>10}{c['skipped_empty']:>15}"
            f"{c['skipped_done']:>14}{c['embedded']:>10}{c['failed']:>8}")
        for col in _SUMMARY_COLS:
            totals[col] += c[col]
    if len(results) > 1:
        log(f"{'TOTAL':<18}{totals['scanned']:>10}{totals['skipped_empty']:>15}"
            f"{totals['skipped_done']:>14}{totals['embedded']:>10}{totals['failed']:>8}")


# ---------------------------------------------------------------- CLI


_TARGET_HELP = "\n".join(
    f"  {k:<18} {t.label}.{t.text_prop} -> {t.vector_prop}"
    for k, t in TARGETS.items()
)

_USAGE_EXAMPLES = """\
示例：
  python scripts/backfill_vectors.py --dry-run
  python scripts/backfill_vectors.py --targets basis,citation_name
  python scripts/backfill_vectors.py --service-ids ids.txt --targets service,material,faq_answer
  python scripts/backfill_vectors.py --targets service --limit 8 --batch-size 8
"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_vectors",
        description="为 zwdmxgj 图中被 mock 零向量污染的向量属性回填真实 bge-m3 嵌入",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="回填目标（--targets 逗号分隔，默认全部）：\n"
               + _TARGET_HELP + "\n\n" + _USAGE_EXAMPLES,
    )
    parser.add_argument("--targets", metavar="KEYS",
                        default=",".join(TARGETS),
                        help="逗号分隔的回填目标键，默认全部：" + ",".join(TARGETS))
    parser.add_argument("--service-ids", metavar="FILE",
                        help="每行一个 serviceId 的文件（或 - 读 stdin）；只回填这些事项"
                             "及其关联实体（requiresMaterial/citesLegal/partOf(2跳)/"
                             "hasCondition/hasFaq/hasChunk 扩展）")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="每个 target 最多成功嵌入写回的节点数（默认不限；用于冒烟）")
    parser.add_argument("--batch-size", type=int, default=64, metavar="N",
                        help="每批嵌入/写回的节点数（默认 64）")
    parser.add_argument("--include-nonzero", action="store_true",
                        help="向量已非零的节点也重新嵌入（默认跳过，幂等；"
                             "重刷请配合新的 --state-file）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计将回填的节点，不调 ollama、不写图、不写 state")
    parser.add_argument("--batch-api", action="store_true",
                        help="用 ollama 批量端点 /api/embed 一次嵌入整批（GPU 并行，约 25 倍吞吐；需 ollama 0.2.6+）")
    parser.add_argument("--max-chars", type=int, default=0, metavar="N",
                        help="嵌入前文本截断上限（字符，0=不截断）；长法条/长条件超出 bge-m3 8192 token 上下文时使用，如 4000")
    parser.add_argument("--state-file", metavar="PATH", default=str(DEFAULT_STATE_FILE),
                        help="断点 state 文件（JSON Lines，默认 %(default)s）")
    args = parser.parse_args(argv)

    keys = [k.strip() for k in args.targets.split(",") if k.strip()]
    unknown = [k for k in keys if k not in TARGETS]
    if unknown:
        parser.error(f"未知 target：{','.join(unknown)}（可选：{','.join(TARGETS)}）")
    if not keys:
        parser.error("--targets 不能为空")
    if args.batch_size < 1:
        parser.error("--batch-size 必须 >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须 >= 1")
    args.target_keys = keys
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    service_ids: list[str] | None = None
    if args.service_ids:
        try:
            service_ids = read_service_ids(args.service_ids)
        except (OSError, ValueError) as e:
            print(f"[错误] 读取 --service-ids 失败：{e}", file=sys.stderr)
            return 2

    state = BackfillState(args.state_file, read_only=args.dry_run)
    results: dict[str, dict[str, int]] = {}
    embedder = OllamaEmbedder(max_chars=args.max_chars, batch_api=args.batch_api)
    subset_desc = f"{len(service_ids)} 个 serviceId 子集" if service_ids else "全量"
    print(f"# Neo4j={NEO4J_URI} db={NEO4J_DB}；ollama={OLLAMA_URL} "
          f"model={EMBED_MODEL}（{EMBED_DIM} 维）")
    print(f"# targets={','.join(args.target_keys)} 范围={subset_desc} "
          f"batch_size={args.batch_size} limit={args.limit} "
          f"include_nonzero={args.include_nonzero} dry_run={args.dry_run} "
          f"state={args.state_file}{'（只读）' if args.dry_run else ''}")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            with driver.session(database=NEO4J_DB) as session:
                for key in args.target_keys:
                    results[key] = process_target(
                        TARGETS[key], session=session, embedder=embedder, state=state,
                        service_ids=service_ids, batch_size=args.batch_size,
                        limit=args.limit, include_nonzero=args.include_nonzero,
                        dry_run=args.dry_run)
        finally:
            driver.close()
    except KeyboardInterrupt:
        print(f"\n[中断] Ctrl-C：进度已持久化到 {args.state_file}，"
              f"重跑同一命令将跳过已完成节点。", file=sys.stderr)
        print_summary(results, args.dry_run)
        return 130
    except EmbedServiceError as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        print_summary(results, args.dry_run)
        return 2
    except _NEO4J_FATAL as e:  # type: ignore[misc]
        print(f"\n[错误] Neo4j 执行失败（{NEO4J_URI}, db={NEO4J_DB}）：{e}", file=sys.stderr)
        print_summary(results, args.dry_run)
        return 2
    finally:
        state.close()

    print_summary(results, args.dry_run)
    return 0 if all(c["failed"] == 0 for c in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
