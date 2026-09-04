#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/backfill_vectors.py 单测（不连真实 Neo4j、不调真实 ollama）。

覆盖（与 tests/test_schema_switch.py 相同的桩策略：无 neo4j 包环境下装桩模块）：
  1. target 清单与属性映射（标签/文本属性/id 属性/向量属性名规律，不含 ProcessStep）；
  2. --service-ids 关联扩展的 Cypher 语句包含正确标签与关系名（含 basis 两跳）；
  3. 零向量判定函数 is_missing_or_zero；
  4. state（JSON Lines）读写、坏行容忍、只读模式、断点跳过（重跑不再嵌入）；
  5. 批处理切分 chunked 与嵌入批次划分；
  6. 空文本跳过、非零向量跳过、--include-nonzero、--limit、dry-run 无副作用、
     连续失败中止、服务级错误上抛；
  7. OllamaEmbedder 请求/错误分类（mock urllib.request.urlopen）；
  8. CLI 参数解析与 --service-ids 文件读取。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _ensure_neo4j_importable() -> None:
    """无 neo4j 包的环境下装桩（仅测试进程内，不触真实服务）。"""
    try:
        import neo4j  # noqa: F401
        import neo4j.exceptions  # noqa: F401
    except ImportError:
        stub = types.ModuleType("neo4j")
        stub.GraphDatabase = types.SimpleNamespace(driver=lambda *a, **k: None)
        exceptions = types.ModuleType("neo4j.exceptions")

        class _Neo4jError(Exception):
            pass

        class _ServiceUnavailable(_Neo4jError):
            pass

        class _AuthError(_Neo4jError):
            pass

        exceptions.Neo4jError = _Neo4jError
        exceptions.ServiceUnavailable = _ServiceUnavailable
        exceptions.AuthError = _AuthError
        stub.exceptions = exceptions
        sys.modules["neo4j"] = stub
        sys.modules["neo4j.exceptions"] = exceptions


_ensure_neo4j_importable()

from scripts import backfill_vectors as bf  # noqa: E402


# ---------------------------------------------------------------- 桩件


class FakeResult(list):
    def consume(self):  # 对应 neo4j Result.consume()
        return None


class FakeSession:
    """记录 run() 的 Cypher 与参数；按 keyset 分页（WHERE id > $after）回放 rows。"""

    def __init__(self, rows, count=None):
        self.rows = sorted(rows, key=lambda r: r["id"])  # 与 ORDER BY id 一致
        self.count = len(self.rows) if count is None else count
        self.read_queries: list[str] = []
        self.write_queries: list[str] = []
        self.write_rows: list[list[dict]] = []
        self.all_queries: list[tuple[str, dict]] = []

    def run(self, query, **params):
        self.all_queries.append((query, params))
        if "RETURN count(" in query:
            return FakeResult([{"c": self.count}])
        if query.lstrip().startswith("UNWIND"):
            self.write_queries.append(query)
            self.write_rows.append(params.get("rows"))
            return FakeResult([])
        after = params.get("after", "")
        page = params.get("page", max(len(self.rows), 1))
        selected = [r for r in self.rows if r["id"] > after][:page]
        self.read_queries.append(query)
        return FakeResult(selected)


class FakeEmbedder:
    """ok=固定向量；fail=全部单条失败（None）；down=服务级错误直接上抛。"""

    def __init__(self, mode="ok", dim=4):
        self.mode = mode
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        if self.mode == "down":
            raise bf.EmbedServiceError(
                "无法连接 ollama（http://127.0.0.1:11434/api/embeddings）：connection refused")
        if self.mode == "fail":
            return [None for _ in texts]
        return [[0.25] * self.dim for _ in texts]


def _row(rid, text, vec):
    return {"id": rid, "text": text, "vec": vec}


def _sink():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------- target 清单与属性映射


class TargetMappingTests(unittest.TestCase):
    def test_targets_cover_expected_keys(self) -> None:
        self.assertEqual(
            set(bf.TARGETS),
            {"service", "material", "citation_name", "citation_content", "basis",
             "condition", "faq_name", "faq_answer", "chunk"},
        )

    def test_target_property_mapping(self) -> None:
        expected = {
            "service": ("ZwdmxGJ.GovernmentService", "name", "serviceId", "_name_vector"),
            "material": ("ZwdmxGJ.Material", "name", "materialId", "_name_vector"),
            "citation_name": ("ZwdmxGJ.LegalCitation", "name", "citationId", "_name_vector"),
            "citation_content": ("ZwdmxGJ.LegalCitation", "content", "citationId",
                                 "_content_vector"),
            "basis": ("ZwdmxGJ.LegalBasis", "name", "legalBasisId", "_name_vector"),
            "condition": ("ZwdmxGJ.ServiceCondition", "name", "conditionId", "_name_vector"),
            "faq_name": ("ZwdmxGJ.FAQ", "name", "faqId", "_name_vector"),
            "faq_answer": ("ZwdmxGJ.FAQ", "answer", "faqId", "_answer_vector"),
            "chunk": ("ZwdmxGJ.Chunk", "content", "chunkId", "_content_vector"),
        }
        for key, (label, text_prop, id_prop, vector_prop) in expected.items():
            with self.subTest(key=key):
                t = bf.TARGETS[key]
                self.assertEqual((t.label, t.text_prop, t.id_prop, t.vector_prop),
                                 (label, text_prop, id_prop, vector_prop))

    def test_vector_prop_rule_for_statement_and_check_standard(self) -> None:
        # 实测规律：name→_name_vector、content→_content_vector、answer→_answer_vector；
        # 推断 statement→_statement_vector、checkStandard→_check_standard_vector
        self.assertEqual(bf.vector_prop_for("name"), "_name_vector")
        self.assertEqual(bf.vector_prop_for("content"), "_content_vector")
        self.assertEqual(bf.vector_prop_for("answer"), "_answer_vector")
        self.assertEqual(bf.vector_prop_for("statement"), "_statement_vector")
        self.assertEqual(bf.vector_prop_for("checkStandard"), "_check_standard_vector")

    def test_process_step_excluded(self) -> None:
        for t in bf.TARGETS.values():
            self.assertNotIn("ProcessStep", t.label)


# ---------------------------------------------------------------- Cypher 构建


EXPECTED_EXPANSION = {
    "material": (("requiresMaterial", "ZwdmxGJ.Material"),),
    "citation_name": (("citesLegal", "ZwdmxGJ.LegalCitation"),),
    "citation_content": (("citesLegal", "ZwdmxGJ.LegalCitation"),),
    "basis": (("citesLegal", "ZwdmxGJ.LegalCitation"), ("partOf", "ZwdmxGJ.LegalBasis")),
    "condition": (("hasCondition", "ZwdmxGJ.ServiceCondition"),),
    "faq_name": (("hasFaq", "ZwdmxGJ.FAQ"),),
    "faq_answer": (("hasFaq", "ZwdmxGJ.FAQ"),),
    "chunk": (("hasChunk", "ZwdmxGJ.Chunk"),),
}


class QueryBuildingTests(unittest.TestCase):
    def test_service_expansion_table_matches_spec(self) -> None:
        expected = dict(EXPECTED_EXPANSION, service=())
        self.assertEqual(bf.SERVICE_EXPANSION, expected)

    def test_full_scan_query(self) -> None:
        q = bf.read_query(bf.TARGETS["condition"], subset=False)
        plain = q.replace("`", "")
        self.assertIn("MATCH (n:ZwdmxGJ.ServiceCondition)", plain)
        self.assertIn("n.conditionId > $after", plain)
        self.assertIn("n.name AS text", plain)
        self.assertIn("n._name_vector AS vec", plain)
        self.assertIn("ORDER BY id LIMIT $page", plain)
        self.assertNotIn("$ids", plain)

    def test_subset_service_self_query_filters_by_service_id(self) -> None:
        q = bf.read_query(bf.TARGETS["service"], subset=True)
        plain = q.replace("`", "")
        self.assertIn("MATCH (n:ZwdmxGJ.GovernmentService)", plain)
        self.assertIn("n.serviceId IN $ids", plain)
        self.assertIn("n.serviceId > $after", plain)
        self.assertNotIn(")-[", plain)

    def test_subset_expansion_queries_contain_labels_and_rels(self) -> None:
        for key, hops in EXPECTED_EXPANSION.items():
            with self.subTest(key=key):
                q = bf.read_query(bf.TARGETS[key], subset=True)
                plain = q.replace("`", "")
                self.assertIn("MATCH (s:ZwdmxGJ.GovernmentService)", plain)
                self.assertIn("s.serviceId IN $ids", plain)
                for rel, label in hops:
                    self.assertIn(f"-[:{rel}]->", plain)
                    self.assertIn(f":{label}", plain)
                self.assertIn("DISTINCT", q)
                # 末跳绑定的目标实体即 target 标签
                self.assertIn(f"(n:{bf.TARGETS[key].label})", plain)

    def test_basis_expansion_is_two_hops_in_order(self) -> None:
        q = bf.read_query(bf.TARGETS["basis"], subset=True)
        plain = q.replace("`", "")
        self.assertLess(plain.index("citesLegal"), plain.index("partOf"))
        self.assertIn("-[:citesLegal]->(:ZwdmxGJ.LegalCitation)", plain)
        self.assertIn("-[:partOf]->(n:ZwdmxGJ.LegalBasis)", plain)

    def test_count_query_variants(self) -> None:
        full = bf.count_query(bf.TARGETS["service"], subset=False)
        self.assertIn("MATCH (n:`ZwdmxGJ.GovernmentService`)", full)
        self.assertIn("count(n)", full)
        self_sub = bf.count_query(bf.TARGETS["service"], subset=True)
        self.assertIn("serviceId` IN $ids", self_sub)
        exp = bf.count_query(bf.TARGETS["basis"], subset=True)
        self.assertIn("count(DISTINCT n)", exp)
        self.assertIn("citesLegal", exp)
        self.assertIn("partOf", exp)

    def test_write_query(self) -> None:
        q = bf.write_query(bf.TARGETS["faq_answer"])
        self.assertIn("UNWIND $rows AS row", q)
        self.assertIn("MATCH (n:`ZwdmxGJ.FAQ`) WHERE n.`faqId` = row.id", q)
        self.assertIn("SET n.`_answer_vector` = row.vec", q)


# ---------------------------------------------------------------- 零向量判定 / 切分


class ZeroVectorTests(unittest.TestCase):
    def test_missing(self) -> None:
        self.assertTrue(bf.is_missing_or_zero(None))

    def test_all_zero(self) -> None:
        self.assertTrue(bf.is_missing_or_zero([0.0] * 1024))
        self.assertTrue(bf.is_missing_or_zero([0, 0, 0]))

    def test_nonzero(self) -> None:
        self.assertFalse(bf.is_missing_or_zero([0.0, 1e-9]))
        self.assertFalse(bf.is_missing_or_zero([0.25] * 4))

    def test_empty_or_junk_treated_as_missing(self) -> None:
        self.assertTrue(bf.is_missing_or_zero([]))
        self.assertTrue(bf.is_missing_or_zero("not-a-vector"))
        self.assertTrue(bf.is_missing_or_zero([0.0, "x"]))


class ChunkedTests(unittest.TestCase):
    def test_splits_even(self) -> None:
        self.assertEqual(list(bf.chunked([1, 2, 3, 4], 2)), [[1, 2], [3, 4]])

    def test_smaller_last_chunk(self) -> None:
        self.assertEqual(list(bf.chunked(list(range(5)), 2)),
                         [[0, 1], [2, 3], [4]])

    def test_empty_input(self) -> None:
        self.assertEqual(list(bf.chunked([], 3)), [])

    def test_invalid_size_raises(self) -> None:
        with self.assertRaises(ValueError):
            list(bf.chunked([1], 0))


# ---------------------------------------------------------------- state 读写


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "backfill_state.json"

    def test_missing_file_starts_empty(self) -> None:
        state = bf.BackfillState(self.path)
        self.assertFalse(state.contains("service", "S1"))

    def test_mark_and_reload_roundtrip(self) -> None:
        state = bf.BackfillState(self.path)
        state.mark_many("service", ["S1", "S2"])
        state.mark_many("basis", ["L1"])
        state.close()

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(x) for x in lines],
            [{"target": "service", "id": "S1"},
             {"target": "service", "id": "S2"},
             {"target": "basis", "id": "L1"}],
        )
        reloaded = bf.BackfillState(self.path)
        self.assertTrue(reloaded.contains("service", "S1"))
        self.assertTrue(reloaded.contains("service", "S2"))
        self.assertTrue(reloaded.contains("basis", "L1"))
        self.assertFalse(reloaded.contains("service", "L1"))

    def test_mark_many_is_idempotent(self) -> None:
        state = bf.BackfillState(self.path)
        state.mark_many("service", ["S1"])
        state.mark_many("service", ["S1", "S2"])
        state.close()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_malformed_lines_ignored_with_warning(self) -> None:
        self.path.write_text(
            '{"target": "service", "id": "S1"}\n'
            "{oops 坏行\n"
            '{"target": "service", "id": "S2"}\n',
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            state = bf.BackfillState(self.path)
        self.assertTrue(state.contains("service", "S1"))
        self.assertTrue(state.contains("service", "S2"))
        self.assertIn("1 行无法解析", stderr.getvalue())

    def test_read_only_never_writes_or_creates(self) -> None:
        state = bf.BackfillState(self.path, read_only=True)
        state.mark_many("service", ["S1"])
        state.close()
        self.assertFalse(self.path.exists())
        self.assertFalse(state.contains("service", "S1"))


# ---------------------------------------------------------------- OllamaEmbedder（mock urlopen）


class _FakeHTTPResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, n=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class EmbedderTests(unittest.TestCase):
    def _embedder(self, dim=2):
        return bf.OllamaEmbedder(base_url="http://127.0.0.1:11434",
                                 model="bge-m3", timeout=5, expected_dim=dim)

    def test_embed_one_posts_model_and_prompt(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeHTTPResponse(json.dumps({"embedding": [0.1, 0.2]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            vec = self._embedder().embed_one("办理生育登记")
        self.assertEqual(vec, [0.1, 0.2])
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/embeddings")
        self.assertEqual(captured["body"], {"model": "bge-m3", "prompt": "办理生育登记"})
        self.assertEqual(captured["timeout"], 5)

    def test_http_error_is_service_error_with_hint(self) -> None:
        err = urllib.error.HTTPError("http://x", 404, "Not Found", None,
                                     io.BytesIO("model not found".encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(bf.EmbedServiceError) as ctx:
                self._embedder().embed_one("文本")
        msg = str(ctx.exception)
        self.assertIn("404", msg)
        self.assertIn("bge-m3", msg)
        self.assertIn("ollama pull", msg)

    def test_connection_refused_is_service_error(self) -> None:
        err = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(bf.EmbedServiceError) as ctx:
                self._embedder().embed_one("文本")
        self.assertIn("无法连接 ollama", str(ctx.exception))

    def test_timeout_is_retryable_per_text(self) -> None:
        err = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            embedder = self._embedder()
            with self.assertRaises(bf.EmbedTimeoutError):
                embedder.embed_one("文本")
            # embed_batch 把超时降级为单条失败（None），不整体抛出
            self.assertEqual(embedder.embed_batch(["a", "b"]), [None, None])

    def test_bad_payload_is_service_error(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeHTTPResponse(b"not json")):
            with self.assertRaises(bf.EmbedServiceError):
                self._embedder().embed_one("文本")
        with mock.patch(
                "urllib.request.urlopen",
                return_value=_FakeHTTPResponse(json.dumps({"error": "x"}).encode())):
            with self.assertRaises(bf.EmbedServiceError):
                self._embedder().embed_one("文本")

    def test_dim_mismatch_is_service_error(self) -> None:
        with mock.patch(
                "urllib.request.urlopen",
                return_value=_FakeHTTPResponse(json.dumps({"embedding": [0.5]}).encode())):
            with self.assertRaises(bf.EmbedServiceError) as ctx:
                self._embedder(dim=2).embed_one("文本")
        self.assertIn("维度", str(ctx.exception))


# ---------------------------------------------------------------- process_target 主流程


class ProcessTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "backfill_state.json"

    def _rows(self):
        return [
            _row("S1", "生育登记服务", None),                     # 待回填（向量缺失）
            _row("S2", None, None),                              # 空文本
            _row("S3", "   ", [0.0] * 4),                        # 空白文本
            _row("S4", "已有真实向量", [0.1, 0.2, 0.3, 0.4]),     # 非零，默认跳过
            _row("S5", "上次已完成", [0.0] * 4),                  # state 已记录
        ]

    def _state(self, read_only=False) -> bf.BackfillState:
        return bf.BackfillState(self.state_path, read_only=read_only)

    def test_skips_empty_done_and_nonzero_and_embeds_rest(self) -> None:
        state = self._state()
        state.mark_many("service", ["S5"])
        session = FakeSession(self._rows())
        embedder = FakeEmbedder()
        logs, log = _sink()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=8, log=log)
        state.close()
        self.assertEqual(counters, {"scanned": 5, "skipped_empty": 2,
                                    "skipped_done": 2, "embedded": 1, "failed": 0})
        self.assertEqual(embedder.calls, [["生育登记服务"]])
        self.assertEqual(len(session.write_queries), 1)
        self.assertEqual(session.write_rows[0],
                         [{"id": "S1", "vec": [0.25, 0.25, 0.25, 0.25]}])
        # state 记录了新完成的 S1，且保留原有 S5
        recorded = {json.loads(x)["id"] for x in
                    self.state_path.read_text(encoding="utf-8").splitlines()}
        self.assertEqual(recorded, {"S1", "S5"})
        self.assertTrue(any("1/5" in line for line in logs))

    def test_resume_run_skips_completed_without_embedding(self) -> None:
        state = self._state()
        state.mark_many("service", ["S5"])
        first = FakeSession(self._rows())
        bf.process_target(bf.TARGETS["service"], session=first,
                          embedder=FakeEmbedder(), state=state, batch_size=8,
                          log=lambda *_: None)
        state.close()

        resumed_state = bf.BackfillState(self.state_path)
        second = FakeSession(self._rows())
        embedder = FakeEmbedder()
        counters = bf.process_target(bf.TARGETS["service"], session=second,
                                     embedder=embedder, state=resumed_state,
                                     batch_size=8, log=lambda *_: None)
        resumed_state.close()
        self.assertEqual(embedder.calls, [])  # 断点续跑：不再嵌入
        self.assertEqual(second.write_queries, [])
        self.assertEqual(counters["embedded"], 0)
        self.assertEqual(counters["skipped_done"], 3)  # S1（续跑完成）+ S4（非零）+ S5

    def test_include_nonzero_reembeds(self) -> None:
        session = FakeSession([_row("S4", "已有真实向量", [0.1, 0.2, 0.3, 0.4])])
        embedder = FakeEmbedder()
        state = self._state()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     include_nonzero=True, batch_size=8,
                                     log=lambda *_: None)
        state.close()
        self.assertEqual(counters["embedded"], 1)
        self.assertEqual(counters["skipped_done"], 0)
        self.assertEqual(embedder.calls, [["已有真实向量"]])

    def test_limit_caps_embedded(self) -> None:
        rows = [_row(f"S{i}", f"事项{i}", [0.0] * 4) for i in range(1, 6)]
        session = FakeSession(rows)
        embedder = FakeEmbedder()
        state = self._state()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=2, limit=4, log=lambda *_: None)
        state.close()
        self.assertEqual(counters["embedded"], 4)
        self.assertEqual([len(c) for c in embedder.calls], [2, 2])
        self.assertEqual(len(session.write_rows), 2)

    def test_batches_are_split_by_batch_size(self) -> None:
        rows = [_row(f"S{i}", f"文本{i}", None) for i in range(1, 6)]
        session = FakeSession(rows)
        embedder = FakeEmbedder()
        state = self._state()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=2, log=lambda *_: None)
        state.close()
        self.assertEqual([len(c) for c in embedder.calls], [2, 2, 1])
        self.assertEqual([len(r) for r in session.write_rows], [2, 2, 1])
        self.assertEqual(counters["embedded"], 5)

    def test_all_empty_text_skips_embed_and_write(self) -> None:
        rows = [_row("S1", None, None), _row("S2", "", [0.0] * 4),
                _row("S3", "  ", None)]
        session = FakeSession(rows)
        embedder = FakeEmbedder()
        state = self._state()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=2, log=lambda *_: None)
        state.close()
        self.assertEqual(counters, {"scanned": 3, "skipped_empty": 3,
                                    "skipped_done": 0, "embedded": 0, "failed": 0})
        self.assertEqual(embedder.calls, [])
        self.assertEqual(session.write_queries, [])

    def test_dry_run_touches_nothing(self) -> None:
        seed = self._state()  # 预置一个已完成节点，模拟对已有进度的 dry-run 预览
        seed.mark_many("service", ["S5"])
        seed.close()
        session = FakeSession(self._rows())
        embedder = FakeEmbedder()
        state = self._state(read_only=True)
        logs, log = _sink()
        counters = bf.process_target(bf.TARGETS["service"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=8, dry_run=True, log=log)
        state.close()
        self.assertEqual(counters, {"scanned": 5, "skipped_empty": 2,
                                    "skipped_done": 2, "embedded": 1, "failed": 0})
        self.assertEqual(embedder.calls, [])
        self.assertEqual(session.write_queries, [])
        # dry-run 不追加 state，文件内容保持预置的 S5 一行
        self.assertEqual(self.state_path.read_text(encoding="utf-8").splitlines(),
                         ['{"target": "service", "id": "S5"}'])
        self.assertTrue(any("dry-run" in line for line in logs))

    def test_consecutive_failures_abort(self) -> None:
        rows = [_row(f"S{i}", f"文本{i}", None) for i in range(1, 21)]
        session = FakeSession(rows)
        embedder = FakeEmbedder(mode="fail")
        state = self._state()
        with self.assertRaises(RuntimeError) as ctx:
            bf.process_target(bf.TARGETS["service"], session=session,
                              embedder=embedder, state=state, batch_size=3,
                              log=lambda *_: None)
        state.close()
        self.assertIn("连续", str(ctx.exception))
        self.assertIn("state", str(ctx.exception))
        self.assertGreaterEqual(len(embedder.calls),
                                bf.MAX_CONSECUTIVE_FAILURES // 3)
        self.assertEqual(session.write_queries, [])  # 全失败批次不写图

    def test_service_level_error_propagates(self) -> None:
        session = FakeSession([_row("S1", "文本", None)])
        embedder = FakeEmbedder(mode="down")
        state = self._state()
        with self.assertRaises(bf.EmbedServiceError):
            bf.process_target(bf.TARGETS["service"], session=session,
                              embedder=embedder, state=state, batch_size=8,
                              log=lambda *_: None)
        state.close()

    def test_zero_total_returns_zero_counters(self) -> None:
        session = FakeSession([], count=0)  # chunk 目标当前 0 节点
        embedder = FakeEmbedder()
        state = self._state()
        counters = bf.process_target(bf.TARGETS["chunk"], session=session,
                                     embedder=embedder, state=state,
                                     batch_size=8, log=lambda *_: None)
        state.close()
        self.assertEqual(counters, {"scanned": 0, "skipped_empty": 0,
                                    "skipped_done": 0, "embedded": 0, "failed": 0})
        self.assertEqual(embedder.calls, [])

    def test_subset_mode_passes_service_ids_to_queries(self) -> None:
        session = FakeSession([_row("S1", "文本", None)])
        embedder = FakeEmbedder()
        state = self._state()
        bf.process_target(bf.TARGETS["service"], session=session,
                          embedder=embedder, state=state,
                          service_ids=["SVC-1"], batch_size=8, log=lambda *_: None)
        state.close()
        for query, params in session.all_queries:
            if not query.lstrip().startswith("UNWIND"):  # 写回查询只带 rows
                self.assertEqual(params.get("ids"), ["SVC-1"])


# ---------------------------------------------------------------- --service-ids 文件读取


class ServiceIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_read_file_dedups_strips_and_skips_comments(self) -> None:
        p = Path(self._tmp.name) / "ids.txt"
        p.write_text(" S1 \nS2\n\n# 注释\nS1\n", encoding="utf-8-sig")
        self.assertEqual(bf.read_service_ids(str(p)), ["S1", "S2"])

    def test_read_dash_reads_stdin(self) -> None:
        fake_stdin = types.SimpleNamespace(read=lambda: "S1\n S2 \n#c\nS1\n")
        with mock.patch("sys.stdin", fake_stdin):
            self.assertEqual(bf.read_service_ids("-"), ["S1", "S2"])

    def test_empty_input_raises(self) -> None:
        p = Path(self._tmp.name) / "empty.txt"
        p.write_text("\n# 只有注释\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            bf.read_service_ids(str(p))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(OSError):
            bf.read_service_ids(str(Path(self._tmp.name) / "nope.txt"))


# ---------------------------------------------------------------- CLI


class ParseArgsTests(unittest.TestCase):
    def test_default_targets_all(self) -> None:
        args = bf.parse_args([])
        self.assertEqual(args.target_keys, list(bf.TARGETS))
        self.assertEqual(args.batch_size, 64)
        self.assertIsNone(args.limit)
        self.assertFalse(args.include_nonzero)
        self.assertFalse(args.dry_run)

    def test_targets_subset_and_flags(self) -> None:
        args = bf.parse_args(["--targets", "basis, citation_name",
                              "--batch-size", "8", "--limit", "3",
                              "--include-nonzero", "--dry-run"])
        self.assertEqual(args.target_keys, ["basis", "citation_name"])
        self.assertEqual(args.batch_size, 8)
        self.assertEqual(args.limit, 3)
        self.assertTrue(args.include_nonzero)
        self.assertTrue(args.dry_run)

    def test_unknown_target_exits(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                bf.parse_args(["--targets", "service,typo"])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_limit_or_batch_size_exits(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bf.parse_args(["--limit", "0"])
            with self.assertRaises(SystemExit):
                bf.parse_args(["--batch-size", "0"])

    def test_state_file_default_under_checkpoints(self) -> None:
        args = bf.parse_args([])
        self.assertEqual(Path(args.state_file).name, "backfill_state.json")
        self.assertIn("checkpoints", Path(args.state_file).parts)


if __name__ == "__main__":
    unittest.main()
