#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/run_baseline.py 单测（不依赖真实 .env / Neo4j / DeepSeek）。

ask 函数全部用桩（monkeypatch load_ask / 直接注入 fake），覆盖：
  1. 题集读取（utf-8-sig BOM、缺列/空 test_id/空文件报错）；
  2. 断点续跑：已有 test_id 跳过、坏行容忍、追加写不破坏旧行；
  3. 单题重试（--retries）：成功前重试、耗尽后写 error 行（answer=""）；
  4. ask() 返回结构异常（非 dict / 缺 answer 字段）按错误重试处理；
  5. --multihop 透传 ask(question, multihop=True)；
  6. --limit 提前停止、每 5 题进度打印、统计口径；
  7. CLI 参数校验与 main() 端到端（monkeypatch load_ask）。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import run_baseline as rb  # noqa: E402


# ---------------------------------------------------------------- 桩件


class FakeAsk:
    """记录调用；前 fail_times 次抛 error，之后返回固定答案结构。"""

    def __init__(self, fail_times: int = 0, error: Exception | None = None,
                 result_factory=None):
        self.fail_times = fail_times
        self.error = error or RuntimeError("deepseek 连接失败")
        self.result_factory = result_factory or (lambda q: {"answer": f"答案:{q}",
                                                            "usage": {}, "seeds": []})
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, question: str, debug: bool = False, multihop: bool = False):
        self.calls.append((question, multihop))
        if len(self.calls) <= self.fail_times:
            raise self.error
        return self.result_factory(question)


def _sink():
    lines: list[str] = []
    return lines, lines.append


def _write_testset(path: Path, rows) -> Path:
    import csv
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["test_id", "question"])
        writer.writerows(rows)
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


# ---------------------------------------------------------------- 题集读取


class ReadTestsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_reads_bom_csv(self) -> None:
        path = _write_testset(self.dir / "t.csv", [["MH001", "问句一？"], ["MH002", "问句二？"]])
        rows = rb.read_testset(path)
        self.assertEqual(rows, [{"test_id": "MH001", "question": "问句一？"},
                                {"test_id": "MH002", "question": "问句二？"}])

    def test_missing_file_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            rb.read_testset(self.dir / "nope.csv")

    def test_missing_columns_raise_valueerror(self) -> None:
        p = self.dir / "bad.csv"
        p.write_text("test_id,other\nMH001,x\n", encoding="utf-8-sig")
        with self.assertRaises(ValueError) as ctx:
            rb.read_testset(p)
        self.assertIn("question", str(ctx.exception))

    def test_blank_test_id_or_question_raises(self) -> None:
        p = _write_testset(self.dir / "b1.csv", [["", "有问题"]])
        with self.assertRaises(ValueError):
            rb.read_testset(p)
        p2 = _write_testset(self.dir / "b2.csv", [["MH001", "   "]])
        with self.assertRaises(ValueError):
            rb.read_testset(p2)

    def test_empty_file_raises(self) -> None:
        p = _write_testset(self.dir / "e.csv", [])
        with self.assertRaises(ValueError):
            rb.read_testset(p)


class LoadDoneIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(rb.load_done_ids(self.dir / "nope.jsonl"), set())

    def test_reads_done_ids_and_tolerates_bad_lines(self) -> None:
        p = self.dir / "pred.jsonl"
        p.write_text(
            '{"test_id": "MH001", "answer": "a"}\n'
            "不是JSON\n"
            '{"test_id": "MH002", "answer": "", "error": "x"}\n'
            "\n"
            '{"answer": "无id行"}\n',
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            done = rb.load_done_ids(p)
        self.assertEqual(done, {"MH001", "MH002"})   # error 行也算已完成（跳过）
        self.assertIn("JSON 解析失败", stderr.getvalue())


# ---------------------------------------------------------------- ask_one / run


class AskOneTests(unittest.TestCase):
    def test_success_first_attempt(self) -> None:
        ask = FakeAsk()
        outcome = rb.ask_one(ask, "问句", multihop=True, retries=2)
        self.assertEqual(outcome, {"answer": "答案:问句", "attempts": 1})
        self.assertEqual(ask.calls, [("问句", True)])

    def test_retries_then_success(self) -> None:
        ask = FakeAsk(fail_times=2)
        outcome = rb.ask_one(ask, "问句", multihop=False, retries=2)
        self.assertEqual(outcome, {"answer": "答案:问句", "attempts": 3})

    def test_exhausted_retries_write_error(self) -> None:
        ask = FakeAsk(fail_times=10**9, error=ValueError("连接超时"))
        outcome = rb.ask_one(ask, "问句", multihop=False, retries=2)
        self.assertEqual(outcome["answer"], "")
        self.assertEqual(outcome["attempts"], 3)   # 1 + retries
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("连接超时", outcome["error"])
        self.assertEqual(len(ask.calls), 3)

    def test_zero_retries_single_attempt(self) -> None:
        ask = FakeAsk(fail_times=1)
        outcome = rb.ask_one(ask, "问句", multihop=False, retries=0)
        self.assertIn("error", outcome)
        self.assertEqual(len(ask.calls), 1)

    def test_bad_result_shape_counts_as_error(self) -> None:
        ask = FakeAsk(result_factory=lambda q: {"no_answer_field": 1})
        outcome = rb.ask_one(ask, "问句", multihop=False, retries=1)
        self.assertEqual(outcome["answer"], "")
        self.assertIn("answer", outcome["error"])
        ask2 = FakeAsk(result_factory=lambda q: "not-a-dict")
        outcome2 = rb.ask_one(ask2, "问句", multihop=False, retries=0)
        self.assertIn("dict", outcome2["error"])


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.rows = [{"test_id": f"MH00{i}", "question": f"问句{i}？"} for i in range(1, 4)]

    def _testset(self):
        return _write_testset(self.dir / "t.csv",
                              [[r["test_id"], r["question"]] for r in self.rows])

    def test_appends_jsonl_lines(self) -> None:
        out = self.dir / "pred.jsonl"
        ask = FakeAsk()
        stats = rb.run(self.rows, out, ask, retries=1, sleep=0, log=lambda _: None)
        records = _read_jsonl(out)
        self.assertEqual([r["test_id"] for r in records], ["MH001", "MH002", "MH003"])
        self.assertEqual(records[0]["answer"], "答案:问句1？")
        self.assertNotIn("error", records[0])
        self.assertEqual(stats, {"total": 3, "skipped_done": 0, "ok": 3,
                                 "failed": 0, "retried": 0})

    def test_resume_skips_existing_test_ids(self) -> None:
        out = self.dir / "pred.jsonl"
        out.write_text(json.dumps({"test_id": "MH001", "answer": "旧答案"},
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        ask = FakeAsk()
        stats = rb.run(self.rows, out, ask, retries=1, sleep=0, log=lambda _: None)
        records = _read_jsonl(out)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["answer"], "旧答案")   # 旧行原样保留
        self.assertEqual([q for q, _ in ask.calls], ["问句2？", "问句3？"])
        self.assertEqual(stats["skipped_done"], 1)
        self.assertEqual(stats["ok"], 2)

    def test_failed_row_structure(self) -> None:
        out = self.dir / "pred.jsonl"
        ask = FakeAsk(fail_times=10**9, error=RuntimeError("服务不可用"))
        stats = rb.run(self.rows, out, ask, retries=2, sleep=0, log=lambda _: None)
        records = _read_jsonl(out)
        self.assertEqual(len(records), 3)
        for record, row in zip(records, self.rows):
            self.assertEqual(record["test_id"], row["test_id"])
            self.assertEqual(record["answer"], "")
            self.assertIn("RuntimeError: 服务不可用", record["error"])
        self.assertEqual((stats["ok"], stats["failed"], stats["retried"]), (0, 3, 6))

    def test_retry_then_success_within_run(self) -> None:
        out = self.dir / "pred.jsonl"
        ask = FakeAsk(fail_times=2)   # 前两次失败，第三种状态（第 1 题）成功
        stats = rb.run(self.rows, out, ask, retries=2, sleep=0, log=lambda _: None)
        self.assertEqual(stats["ok"], 3)   # 第 1 题用满 3 次尝试后成功
        self.assertEqual(stats["retried"], 2)
        self.assertEqual(len(ask.calls), 5)   # 3 + 1 + 1

    def test_limit_stops_early(self) -> None:
        out = self.dir / "pred.jsonl"
        ask = FakeAsk()
        stats = rb.run(self.rows, out, ask, limit=2, retries=0, sleep=0, log=lambda _: None)
        self.assertEqual(len(_read_jsonl(out)), 2)
        self.assertEqual((stats["ok"], stats["failed"]), (2, 0))
        self.assertEqual(len(ask.calls), 2)

    def test_multihop_flag_propagated(self) -> None:
        ask = FakeAsk()
        rb.run(self.rows[:1], self.dir / "a.jsonl", ask, multihop=True,
               retries=0, sleep=0, log=lambda _: None)
        self.assertEqual(ask.calls[0][1], True)
        ask2 = FakeAsk()
        rb.run(self.rows[:1], self.dir / "b.jsonl", ask2, multihop=False,
               retries=0, sleep=0, log=lambda _: None)
        self.assertEqual(ask2.calls[0][1], False)

    def test_progress_logged_every_five(self) -> None:
        rows = [{"test_id": f"MH0{i:02d}", "question": f"问{i}"} for i in range(1, 8)]
        out = self.dir / "pred.jsonl"
        logs, log = _sink()
        rb.run(rows, out, FakeAsk(), retries=0, sleep=0, log=log)
        self.assertTrue(any(line.startswith("[5/7]") for line in logs))
        self.assertTrue(any(line.startswith("[7/7]") for line in logs))
        self.assertFalse(any(line.startswith("[4/7]") for line in logs))

    def test_sleep_paces_between_questions(self) -> None:
        out = self.dir / "pred.jsonl"
        with mock.patch("time.sleep") as sleep_mock:
            rb.run(self.rows, out, FakeAsk(), retries=0, sleep=0.25, log=lambda _: None)
        self.assertEqual(sleep_mock.call_count, 3)
        sleep_mock.assert_called_with(0.25)


# ---------------------------------------------------------------- CLI 与 main


class ParseArgsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = rb.parse_args(["--testset", "t.csv", "--out", "o.jsonl"])
        self.assertFalse(args.multihop)
        self.assertIsNone(args.limit)
        self.assertEqual((args.retries, args.sleep), (2, 0.5))

    def test_invalid_values_exit(self) -> None:
        with redirect_stderr(io.StringIO()):
            for argv in (["--testset", "t.csv", "--out", "o.jsonl", "--retries", "-1"],
                         ["--testset", "t.csv", "--out", "o.jsonl", "--sleep", "-0.1"],
                         ["--testset", "t.csv", "--out", "o.jsonl", "--limit", "0"]):
                with self.assertRaises(SystemExit) as ctx:
                    rb.parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_required_options(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                rb.parse_args(["--out", "o.jsonl"])
            with self.assertRaises(SystemExit):
                rb.parse_args(["--testset", "t.csv"])


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_end_to_end_with_stubbed_ask(self) -> None:
        testset = _write_testset(self.dir / "t.csv",
                                 [["MH001", "问句一"], ["MH002", "问句二"]])
        out = self.dir / "pred.jsonl"
        ask = FakeAsk()
        with mock.patch.object(rb, "load_ask", return_value=ask) as load_mock:
            code = rb.main(["--testset", str(testset), "--out", str(out),
                            "--multihop", "--retries", "1", "--sleep", "0"])
        self.assertEqual(code, 0)
        load_mock.assert_called_once()
        records = _read_jsonl(out)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(mh is True for _, mh in ask.calls))

    def test_main_resumes_and_exits_zero(self) -> None:
        testset = _write_testset(self.dir / "t.csv",
                                 [["MH001", "问句一"], ["MH002", "问句二"]])
        out = self.dir / "pred.jsonl"
        out.write_text(json.dumps({"test_id": "MH001", "answer": "x"},
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        ask = FakeAsk()
        with mock.patch.object(rb, "load_ask", return_value=ask):
            code = rb.main(["--testset", str(testset), "--out", str(out), "--sleep", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(len(_read_jsonl(out)), 2)

    def test_missing_testset_returns_two(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = rb.main(["--testset", str(self.dir / "nope.csv"),
                            "--out", str(self.dir / "o.jsonl")])
        self.assertEqual(code, 2)
        self.assertIn("不存在", stderr.getvalue())

    def test_load_ask_resolves_qa_module(self) -> None:
        # load_ask 只做 sys.path 注入 + 延迟导入；用桩 ask 模块验证解析逻辑，
        # 不触碰真实 qa/（其导入即读 KG_SCHEMA 等环境变量并初始化客户端配置）。
        fake_mod = types.ModuleType("ask")

        def fake_ask(question, debug=False, multihop=False):
            return {"answer": "ok"}

        fake_mod.ask = fake_ask
        saved_path = list(sys.path)
        try:
            with mock.patch.dict(sys.modules, {"ask": fake_mod}):
                resolved = rb.load_ask()
            self.assertIs(resolved, fake_ask)
            self.assertEqual(resolved("问句"), {"answer": "ok"})
        finally:
            sys.path[:] = saved_path   # 不让 qa/ 路径泄漏到其他测试


if __name__ == "__main__":
    unittest.main()
