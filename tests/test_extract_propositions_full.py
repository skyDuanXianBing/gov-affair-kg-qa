#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/extract_propositions_full.py 单测（不连真实 LLM / 不读 17GB 输入）。

覆盖：
  1. DoneState：JSONL 加载去重、末行写坏容忍、mark/flush 追加；
  2. OutputCsv：新文件写 BOM+列头，追加已有文件不在中部插 BOM；
  3. main() 端到端（FakeClient + 临时小 CSV）：exit 0、输出列正确、
     state 行数 = ok+empty、failed 块不记 done；
  4. 断点续跑：重跑同参数全部跳过（0 新增 state 行）；
  5. 失败块退出码 3；换好客户端重跑补齐后 exit 0；
  6. stop_event 预置 → exit 130、不写任何 state 行；
  7. 并发有界：峰值并发 ≤ workers。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import extract_propositions_full as ef  # noqa: E402


# ---------------------------------------------------------------- 桩件


class FakeClient:
    """线程安全 LLM 桩：按用户消息中的标记决定行为。

    含 fail_marker → 抛 TimeoutError（可重试，模拟网络故障）；
    含 empty_marker → 返回 "[]"（空抽）；否则返回 1 条合法命题。
    """

    endpoint = "http://fake/v1/chat/completions"
    model = "fake-model"

    def __init__(self, fail_marker="FAILBLOCK", empty_marker="EMPTYBLOCK",
                 delay: float = 0.0):
        self.fail_marker = fail_marker
        self.empty_marker = empty_marker
        self.delay = delay
        self.calls = 0
        self.peak = 0
        self._active = 0
        self._lock = threading.Lock()

    def chat(self, messages: list[dict[str, str]]) -> str:
        with self._lock:
            self.calls += 1
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            user = messages[-1]["content"]
            if self.fail_marker in user:
                raise TimeoutError("fake timeout")
            if self.empty_marker in user:
                return "[]"
            return json.dumps([{
                "predicate": "资格要求",
                "objectValue": "符合国家规定",
                "statement": "高等学校设立应当符合国家规定的设置标准。",
            }], ensure_ascii=False)
        finally:
            with self._lock:
                self._active -= 1


# ---------------------------------------------------------------- 工具


def write_chunks_csv(path: Path) -> None:
    """6 行小 CSV：正常 / 空抽 / 失败 / 短块 / 无 chunk_id / 正常。"""
    import csv
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["chunk_id", "doc_id", "chunk_no", "title", "content",
                    "category_l1", "category_l2", "service_id"])
        w.writerow(["c:001", "d:1", "1", "事项甲",
                    "受理条件：高等学校设立应当符合国家规定的设置标准。", "法人服务",
                    "教育", "s:1"])
        w.writerow(["c:002", "d:1", "2", "事项甲",
                    "EMPTYBLOCK 办理流程：窗口提交申请材料。", "法人服务", "教育",
                    "s:1"])
        w.writerow(["c:003", "d:1", "3", "事项甲",
                    "FAILBLOCK 办理条件：需要提交可行性报告。", "法人服务", "教育",
                    "s:1"])
        w.writerow(["c:004", "d:1", "4", "事项甲", "短", "法人服务", "教育", "s:1"])
        w.writerow(["", "d:1", "5", "事项甲",
                    "办理地点：政务服务中心二楼综合窗口。", "法人服务", "教育", "s:1"])
        w.writerow(["c:006", "d:2", "1", "事项乙",
                    "申请材料：营业执照副本复印件一份。", "自然人服务", "社保", "s:2"])


def run_main(tmp: Path, client, *extra: str,
             stop_event: threading.Event | None = None,
             chunks_name: str = "chunks.csv") -> tuple[int, list[str]]:
    chunks = tmp / chunks_name
    if not chunks.exists():
        write_chunks_csv(chunks)
    out = tmp / "props.csv"
    state = tmp / "state.jsonl"
    logs: list[str] = []
    code = ef.main(
        argv=["--chunks", str(chunks), "--out", str(out),
              "--state-file", str(state), "--workers", "2", "--retries", "1",
              "--min-chunk-chars", "5", "--log-every", "1",
              "--state-flush-every", "2", *extra],
        client_factory=lambda **kw: client, log=logs.append,
        stop_event=stop_event)
    return code, logs


def read_state(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------- 用例


class TestDoneState(unittest.TestCase):

    def test_load_dedup_and_truncated_tail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.jsonl"
            p.write_text(
                '{"chunk_id": "a", "status": "ok"}\n'
                '{"chunk_id": "b", "status": "empty"}\n'
                '{"chunk_id": "a", "status": "ok"}\n'
                '{"chunk_id": "c", "status": "ok", "trunc',  # 末行写坏
                encoding="utf-8")
            st = ef.DoneState(p)
            self.assertEqual(st.done, {"a", "b"})
            st.mark("d", "ok")
            st.flush()
            st.close()
            lines = p.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 5)  # 追加不重写历史行
            self.assertEqual(json.loads(lines[-1]),
                             {"chunk_id": "d", "status": "ok"})

    def test_failed_status_not_loaded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.jsonl"
            p.write_text('{"chunk_id": "x", "status": "failed"}\n',
                         encoding="utf-8")
            st = ef.DoneState(p)
            self.assertEqual(st.done, set())


class TestOutputCsv(unittest.TestCase):

    def test_fresh_then_append_no_mid_bom(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.csv"
            oc = ef.OutputCsv(p, ["a", "b"])
            oc.writerows([["1", "2"]])
            oc.close()
            self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))
            oc = ef.OutputCsv(p, ["a", "b"])  # 追加模式
            oc.writerows([["3", "4"]])
            oc.close()
            raw = p.read_bytes()
            self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)  # 中部不得再插 BOM
            text = raw.decode("utf-8-sig")
            self.assertEqual([line for line in text.splitlines()],
                             ["a,b", "1,2", "3,4"])


class TestMainEndToEnd(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="ef_e2e_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_exit0_columns_state_accounting(self):
        client = FakeClient()
        code, logs = run_main(self._tmp, client)
        self.assertEqual(code, 3)  # c:003 永远失败 → 3
        out = self._tmp / "props.csv"
        import csv as csv_mod
        with (out).open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv_mod.reader(fh))
        self.assertEqual(rows[0], ["chunk_id", "service_id", "doc_id",
                                   "source_field", "predicate",
                                   "condition_type", "object_value",
                                   "statement"])
        # c:001 与 c:006 各 1 条命题（c:002 空抽 0 行，c:003 失败 0 行）
        self.assertEqual(len(rows) - 1, 2)
        records = read_state(self._tmp / "state.jsonl")
        by_id = {r["chunk_id"]: r["status"] for r in records}
        self.assertEqual(by_id.get("c:001"), "ok")
        self.assertEqual(by_id.get("c:002"), "empty")
        self.assertEqual(by_id.get("c:006"), "ok")
        self.assertNotIn("c:003", by_id)  # failed 不记 done
        # 重试 1 次 → 失败块共 2 次调用
        self.assertEqual(client.calls, 2 + 2 + 1)  # 2 正常 + 2 失败重试 + 1 空抽

    def test_resume_all_skipped(self):
        good = FakeClient(fail_marker="__never__")
        code, _ = run_main(self._tmp, good)  # 全部成功
        self.assertEqual(code, 0)
        before = read_state(self._tmp / "state.jsonl")
        code2, logs2 = run_main(self._tmp, FakeClient())
        self.assertEqual(code2, 0)
        after = read_state(self._tmp / "state.jsonl")
        self.assertEqual(len(before), len(after))  # 0 新增
        self.assertIn("断点跳过 4", " ".join(logs2))

    def test_failed_block_retried_next_round(self):
        # 第一轮：c:003 失败 → exit 3；第二轮换好客户端 → 补齐 exit 0
        code1, _ = run_main(self._tmp, FakeClient())
        self.assertEqual(code1, 3)
        code2, _ = run_main(self._tmp, FakeClient(fail_marker="__never__"))
        self.assertEqual(code2, 0)
        by_id = {r["chunk_id"]: r["status"]
                 for r in read_state(self._tmp / "state.jsonl")}
        self.assertEqual(set(by_id), {"c:001", "c:002", "c:003", "c:006"})

    def test_stop_event_interrupt(self):
        stop = threading.Event()
        stop.set()
        code, logs = run_main(self._tmp, FakeClient(), stop_event=stop)
        self.assertEqual(code, 130)
        self.assertEqual(read_state(self._tmp / "state.jsonl"), [])
        self.assertIn("[中断]", " ".join(logs))

    def test_concurrency_bounded_by_workers(self):
        import csv as csv_mod
        chunks = self._tmp / "many.csv"
        with chunks.open("w", encoding="utf-8", newline="") as fh:
            w = csv_mod.writer(fh)
            w.writerow(["chunk_id", "doc_id", "chunk_no", "title", "content",
                        "category_l1", "category_l2", "service_id"])
            for i in range(12):
                w.writerow([f"c:{i:03d}", "d:1", "1", "事项",
                            f"受理条件：第 {i} 块内容，长度满足最低阈值。",
                            "法人服务", "教育", "s:1"])
        client = FakeClient(delay=0.03)
        code, _ = run_main(self._tmp, client, chunks_name="many.csv")  # workers=2
        self.assertEqual(code, 0)
        self.assertLessEqual(client.peak, 2)


if __name__ == "__main__":
    unittest.main()
