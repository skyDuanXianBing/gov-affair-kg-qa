#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/extract_propositions_pilot.py 单测（不连真实 LLM / 不读 17GB 输入）。

覆盖：
  1. Prompt 常量：受控谓词全集、JSON 数组指令、条件类版含 conditionType 六类；
  2. build_messages：条件类来源换用条件版系统提示；用户消息含事项名/字段/原文；
  3. JSON 解析：裸数组 / ```json 围栏 / 杂文前后缀 / 空数组 / 坏 JSON 报错；
  4. 谓词归一：非法/空谓词 → "其他"并计数；
  5. statement 校验（非空 ≥10 字）与块内去重；
  6. conditionType 仅条件类来源保留，且必须六选一（非法置空计数）；
  7. derive_source_field：标签启发式与优先级；
  8. 分层抽样：短块过滤、层覆盖、limit、seed 复现（mock 流式 reader）；
  9. 名额分配 _fit_quota：总和恰等于 limit、不低于 1、不超蓄水池容量；
 10. state 读写 / 参数匹配判断 / filter_pending 跳过；
 11. 并发归集顺序无关（mock ThreadPoolExecutor.submit + as_completed 乱序）；
 12. extract_one 重试：超时/5xx 重试后成功，4xx 不重试，parse_error 留 raw_head；
 13. main() 端到端（FakeClient + 临时小 CSV）：写 out CSV、state 记账、
     重跑跳过、失败块重跑补齐、state 样本复用免重扫、--sample-only 不调 LLM。
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import extract_propositions_pilot as ep  # noqa: E402


# ---------------------------------------------------------------- 桩件


class FakeClient:
    """可编程 LLM 桩：按调用次序弹出 script（字符串或异常），耗尽用 default。"""

    endpoint = "http://fake/v1/chat/completions"
    model = "fake-model"

    def __init__(self, script: list | None = None, default: str = "[]"):
        self.script = list(script or [])
        self.default = default
        self.calls: list[list[dict[str, str]]] = []
        self.lock = threading.Lock()

    def chat(self, messages):
        with self.lock:
            self.calls.append(messages)
            item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        return item


class RespondingClient(FakeClient):
    """按系统提示类型返回固定命题的桩：条件类带 conditionType，其余不带。"""

    def __init__(self):
        super().__init__(default="")

    def chat(self, messages):
        with self.lock:
            self.calls.append(messages)
        if messages[0]["content"] is ep.SYSTEM_PROMPT_CONDITION:
            return json.dumps([_proposition(condition_type="资格要求")],
                              ensure_ascii=False)
        return json.dumps([_proposition()], ensure_ascii=False)


class FakeFuture:
    """submit 时同步执行任务、稍后可乱序 yield 的假 future。"""

    def __init__(self, fn, args, kwargs):
        self._outcome = None
        self._error = None
        try:
            self._outcome = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self._error = e

    def result(self):
        if self._error is not None:
            raise self._error
        return self._outcome


class FakePool:
    """假线程池：submit 立即（同步）执行并记录 future，供乱序 as_completed。"""

    def __init__(self, max_workers: int = 1):
        self.max_workers = max_workers
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kwargs):
        future = FakeFuture(fn, args, kwargs)
        self.futures.append(future)
        return future


def _reversed_as_completed(futures):
    """假 as_completed：乱序（倒序）yield，验证归集与完成顺序无关。"""
    return iter(list(futures)[::-1])


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://fake", code, "boom", None, None)


def _chunk_row(cid: str, content: str, *, title: str = "测试事项",
               category: str = "法人服务", service: str = "svc-1") -> dict[str, str]:
    return {
        "chunk_id": cid,
        "doc_id": f"doc:{service}",
        "chunk_no": "1",
        "chunk_count": "1",
        "title": title,
        "content": content,
        "category_l1": category,
        "category_l2": "",
        "service_id": service,
        "department_name": "",
        "source_url": "",
        "source_file": "",
        "source_line": "",
        "extras_json": "",
    }


def _sample_item(cid: str, content: str, field: str = "other",
                 category: str = "法人服务") -> dict[str, str]:
    return {
        "chunk_id": cid,
        "doc_id": "doc:svc-1",
        "title": "测试事项",
        "service_id": "svc-1",
        "category_l1": category,
        "source_field": field,
        "chunk_no": "1",
        "content": content,
    }


def _proposition(predicate="办理时限", object_value="20个工作日",
                 statement="测试事项的办理时限为20个工作日。",
                 condition_type=None) -> dict:
    prop = {"predicate": predicate, "objectValue": object_value,
            "statement": statement}
    if condition_type is not None:
        prop["conditionType"] = condition_type
    return prop


def _write_chunks_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ================================================================ Prompt 常量


class TestPromptConstants(unittest.TestCase):

    def test_system_prompt_contains_all_predicates(self):
        for predicate in ep.CONTROLLED_PREDICATES:
            self.assertIn(predicate, ep.SYSTEM_PROMPT)

    def test_system_prompt_instructs_pure_json_array(self):
        self.assertIn("JSON", ep.SYSTEM_PROMPT)
        self.assertIn("数组", ep.SYSTEM_PROMPT)
        self.assertIn("代码块围栏", ep.SYSTEM_PROMPT)  # 明确禁止围栏
        self.assertIn("predicate", ep.SYSTEM_PROMPT)
        self.assertIn("objectValue", ep.SYSTEM_PROMPT)
        self.assertIn("statement", ep.SYSTEM_PROMPT)

    def test_condition_prompt_extends_base_and_lists_types(self):
        self.assertTrue(ep.SYSTEM_PROMPT_CONDITION.startswith(ep.SYSTEM_PROMPT))
        self.assertIn("conditionType", ep.SYSTEM_PROMPT_CONDITION)
        for ct in ep.CONDITION_TYPES:
            self.assertIn(ct, ep.SYSTEM_PROMPT_CONDITION)

    def test_build_messages_switches_by_source_field(self):
        cond = _sample_item("c1", "办理条件：符合有关规定的申请人。", "condition")
        plain = _sample_item("c2", "办理流程：受理、审核、办结。", "process")
        cond_msgs = ep.build_messages(cond)
        plain_msgs = ep.build_messages(plain)
        self.assertEqual(cond_msgs[0]["content"], ep.SYSTEM_PROMPT_CONDITION)
        self.assertEqual(plain_msgs[0]["content"], ep.SYSTEM_PROMPT)
        self.assertIn("测试事项", plain_msgs[1]["content"])
        self.assertIn("来源字段类型", plain_msgs[1]["content"])
        self.assertIn("原文", plain_msgs[1]["content"])
        self.assertIn("受理、审核、办结", plain_msgs[1]["content"])

    def test_build_messages_title_fallback(self):
        item = _sample_item("c3", "任意内容")
        item["title"] = ""
        item["service_id"] = "svc-42"
        msg = ep.build_messages(item)[1]["content"]
        self.assertIn("svc-42", msg)


# ================================================================ JSON 解析


class TestParseJsonArray(unittest.TestCase):

    def test_bare_array(self):
        data = ep.parse_json_array('[{"predicate":"其他","statement":"x"}]')
        self.assertEqual(len(data), 1)

    def test_fenced_array(self):
        text = "```json\n[{\"predicate\":\"其他\"}]\n```"
        data = ep.parse_json_array(text)
        self.assertEqual(data, [{"predicate": "其他"}])

    def test_surrounded_by_noise(self):
        text = '好的，以下是抽取结果：[{"a":1}, {"b":2}] 以上供参考。'
        data = ep.parse_json_array(text)
        self.assertEqual(len(data), 2)

    def test_empty_array(self):
        self.assertEqual(ep.parse_json_array("[]"), [])

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            ep.parse_json_array("[{\"a\": 1,]")

    def test_no_array_raises(self):
        with self.assertRaises(ValueError):
            ep.parse_json_array("抱歉，无法从该段原文中抽取事实。")

    def test_non_string_raises(self):
        with self.assertRaises(ValueError):
            ep.parse_json_array(None)

    def test_non_list_top_level_raises(self):
        with self.assertRaises(ValueError):
            ep.parse_json_array('{"predicate": "其他"}')


# ================================================================ 命题归一


class TestNormalizePropositions(unittest.TestCase):

    def test_valid_propositions_kept(self):
        raw = [_proposition(), _proposition(predicate="收费标准",
                                            object_value="不收费",
                                            statement="测试事项的办理不收费。")]
        props, stats = ep.normalize_propositions(raw, "process")
        self.assertEqual(len(props), 2)
        self.assertEqual(stats["kept"], 2)
        self.assertEqual(props[0]["predicate"], "办理时限")
        self.assertEqual(props[0]["object_value"], "20个工作日")

    def test_invalid_predicate_normalized_to_other(self):
        raw = [_proposition(predicate="受理部门",
                            statement="测试事项由本级主管部门负责受理申请。"),
               _proposition(predicate="", statement="测试事项的办理不收取任何费用。"),
               _proposition(predicate=123, statement="测试事项支持网上全流程办理。")]
        props, stats = ep.normalize_propositions(raw, "process")
        self.assertEqual([p["predicate"] for p in props],
                         ["其他", "其他", "其他"])
        self.assertEqual(stats["invalid_predicate"], 3)

    def test_short_statement_dropped(self):
        raw = [_proposition(statement="太短"),
               _proposition(statement="   "),
               _proposition(statement=None)]
        props, stats = ep.normalize_propositions(raw, "process")
        self.assertEqual(props, [])
        self.assertEqual(stats["bad_statement"], 3)

    def test_duplicate_statement_deduped_keeps_first(self):
        first = _proposition(predicate="办理时限")
        second = _proposition(predicate="其他")
        second["statement"] = first["statement"]
        props, stats = ep.normalize_propositions([first, second], "process")
        self.assertEqual(len(props), 1)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(props[0]["predicate"], "办理时限")

    def test_condition_type_kept_only_for_condition_source(self):
        raw = [_proposition(condition_type="资格要求"),
               _proposition(predicate="时间限制",
                            object_value="3个月内",
                            statement="测试事项应当自受理之日起3个月内作出决定。",
                            condition_type="时间限制")]
        props, stats = ep.normalize_propositions(raw, "condition")
        self.assertEqual([p["condition_type"] for p in props],
                         ["资格要求", "时间限制"])
        self.assertEqual(stats["invalid_condition_type"], 0)
        # 非条件类来源：conditionType 一律丢弃
        props2, _ = ep.normalize_propositions(raw, "process")
        self.assertEqual([p["condition_type"] for p in props2], ["", ""])

    def test_invalid_condition_type_blank_and_counted(self):
        raw = [_proposition(condition_type="前置审批"),
               _proposition(predicate="适用对象",
                            object_value="企业法人",
                            statement="测试事项的申请对象为企业法人。",
                            condition_type=""),
               _proposition(predicate="禁止情形",
                            object_value="失信被执行人",
                            statement="测试事项的申请人不得为失信被执行人。",
                            condition_type="资格要求")]
        props, stats = ep.normalize_propositions(raw, "condition")
        self.assertEqual([p["condition_type"] for p in props],
                         ["", "", "资格要求"])
        self.assertEqual(stats["invalid_condition_type"], 1)

    def test_non_dict_items_dropped(self):
        props, stats = ep.normalize_propositions(["texto", 42], "process")
        self.assertEqual(props, [])
        self.assertEqual(stats["bad_statement"], 2)


# ================================================================ source_field 启发式


class TestDeriveSourceField(unittest.TestCase):

    def test_labels_map_to_fields(self):
        cases = {
            "办理条件：符合有关规定的申请人可以提出申请。": "condition",
            "受理条件：申请人应当具备法人资格。": "condition",
            "办理流程：受理、审核、办结。": "process",
            "窗口办理：工作日 9:00-17:00。": "process",
            "申请材料：申请表、营业执照复印件。": "material",
            "收费标准：不收费。": "fee",
            "办理时限：20个工作日。": "timeLimit",
            "设定依据：《中华人民共和国行政许可法》。": "legalBasis",
            "常见问题：如何补办？": "faq",
            "事项名称：测试事项\n办理条件：符合规定。": "condition",
        }
        for content, expected in cases.items():
            self.assertEqual(ep.derive_source_field(content), expected,
                             msg=content[:20])

    def test_condition_has_priority_over_process(self):
        content = "办理流程：受理。办理条件：符合规定。"
        self.assertEqual(ep.derive_source_field(content), "condition")

    def test_no_label_maps_to_other(self):
        self.assertEqual(ep.derive_source_field("本条是句中截断的普通正文。"),
                         "other")
        self.assertEqual(ep.derive_source_field(""), "other")

    def test_is_condition_source(self):
        self.assertTrue(ep.is_condition_source("condition"))
        self.assertFalse(ep.is_condition_source("process"))


# ================================================================ 分层抽样

# 各字段的基础文本均 ≥ 30 字符（拼接“（第k条补充说明文字）”后更长），
# 保证只考验分层逻辑本身而不被短块过滤干扰。
_FIELD_TEXTS = {
    "condition": "办理条件：符合国家有关规定的申请人，可以向实施机关提出申请，同时提交相关证明材料。",
    "process": "办理流程：申请人提交申请后，窗口受理并进行审核，审核通过后办结并送达办理结果。",
    "material": "申请材料：申请表原件一份、营业执照复印件一份、经办人身份证明复印件一份。",
    "other": "本段是无明显段落标签的普通说明性正文内容，用于测试其他类型的分层抽样逻辑。",
}


class TestStratifiedSample(unittest.TestCase):

    @staticmethod
    def _rows(n_per_field: int = 40) -> list[dict[str, str]]:
        rows = []
        for field, seed_text in _FIELD_TEXTS.items():
            for k in range(n_per_field):
                rows.append(_chunk_row(
                    f"{field}-{k:03d}",
                    seed_text + f"（第{k}条补充说明文字）",
                    category="法人服务" if k % 2 == 0 else "个人服务"))
        rows.append(_chunk_row("short-1", "太短"))          # 短块：过滤
        rows.append(_chunk_row("short-2", ""))              # 空块：过滤
        rows.append(_chunk_row("", _FIELD_TEXTS["condition"]))  # 无 id：跳过
        return rows

    def test_sample_respects_limit_and_filters(self):
        sample, stats = ep.scan_and_sample(
            self._rows(), limit=10, seed=7, min_chars=30, max_chars=1500,
            reservoir_cap=250, log=lambda *_: None)
        self.assertEqual(len(sample), 10)
        self.assertEqual(stats["scanned"], 4 * 40 + 3)
        self.assertEqual(stats["skipped_short"], 2)
        self.assertEqual(stats["skipped_no_id"], 1)
        self.assertTrue(all(len(it["content"]) >= 30 for it in sample))
        self.assertEqual(len({it["chunk_id"] for it in sample}), 10)

    def test_sample_covers_strata_when_pool_small(self):
        # 可用量 ≤ limit 时应全取：4 字段 × 2 类别 = 8 层
        rows = self._rows(n_per_field=2)
        sample, _ = ep.scan_and_sample(
            rows, limit=50, seed=3, min_chars=30, max_chars=1500,
            reservoir_cap=250, log=lambda *_: None)
        self.assertEqual(len(sample), 8)
        strata = {(it["source_field"], it["category_l1"]) for it in sample}
        self.assertEqual(len(strata), 8)

    def test_sample_reproducible_with_same_seed(self):
        rows = self._rows()
        a = ep.scan_and_sample(rows, limit=12, seed=99, min_chars=30,
                               max_chars=1500, reservoir_cap=5,
                               log=lambda *_: None)[0]
        b = ep.scan_and_sample(rows, limit=12, seed=99, min_chars=30,
                               max_chars=1500, reservoir_cap=5,
                               log=lambda *_: None)[0]
        self.assertEqual([it["chunk_id"] for it in a],
                         [it["chunk_id"] for it in b])
        c = ep.scan_and_sample(rows, limit=12, seed=100, min_chars=30,
                               max_chars=1500, reservoir_cap=5,
                               log=lambda *_: None)[0]
        self.assertNotEqual([it["chunk_id"] for it in a],
                            [it["chunk_id"] for it in c])

    def test_content_truncated_to_max_chars(self):
        rows = [_chunk_row("long-1", "办理条件：" + "长" * 5000)]
        sample, _ = ep.scan_and_sample(
            rows, limit=5, seed=1, min_chars=30, max_chars=100,
            reservoir_cap=10, log=lambda *_: None)
        self.assertEqual(len(sample), 1)
        self.assertEqual(len(sample[0]["content"]), 100)
        self.assertEqual(sample[0]["source_field"], "condition")  # 截断后仍有标签


class TestFitQuota(unittest.TestCase):

    def test_quota_sums_to_limit(self):
        totals = {"a|甲": 1000, "b|乙": 500, "c|丙": 100}
        caps = {"a|甲": 250, "b|乙": 250, "c|丙": 250}
        for limit in (1, 2, 3, 7, 100, 750):
            quota = {s: max(1, min(caps[s], int(round(limit * totals[s] / 1600))))
                     for s in totals}
            fitted = ep._fit_quota(quota, caps, limit, totals)
            self.assertEqual(sum(fitted.values()), limit, msg=f"limit={limit}")
            self.assertTrue(all(1 <= q <= caps[s] for s, q in fitted.items()))

    def test_quota_capped_by_reservoir(self):
        totals = {"a|甲": 10_000, "b|乙": 1}
        caps = {"a|甲": 3, "b|乙": 1}
        quota = {"a|甲": 3, "b|乙": 1}
        fitted = ep._fit_quota(quota, caps, 4, totals)
        self.assertEqual(fitted, {"a|甲": 3, "b|乙": 1})

    def test_more_strata_than_limit(self):
        totals = {f"s{i}|x": 10 * i for i in range(6)}
        caps = {s: 5 for s in totals}
        quota = {s: 1 for s in totals}  # 6 层各 1 已超 limit=4
        fitted = ep._fit_quota(quota, caps, 4, totals)
        self.assertEqual(sum(fitted.values()), 4)


# ================================================================ state 与跳过


class TestState(unittest.TestCase):

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cp" / "state.json"
            state = {"model": "m", "prompt_version": "v",
                     "done": ["c1", "c2"],
                     "limit": 10, "seed": 1, "min_chars": 30,
                     "max_chars": 1500, "reservoir_cap": 250,
                     "sample": [_sample_item("c1", "内容")]}
            ep.save_state(path, state)
            loaded = ep.load_state(path)
            self.assertEqual(loaded["done"], ["c1", "c2"])
            self.assertEqual(loaded["sample"][0]["chunk_id"], "c1")

    def test_load_missing_returns_empty(self):
        loaded = ep.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        self.assertEqual(loaded, {"done": []})

    def test_load_corrupt_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken json", encoding="utf-8")
            loaded = ep.load_state(path)
            self.assertEqual(loaded, {"done": []})

    def test_sample_params_match(self):
        state = {"limit": 10, "seed": 1, "min_chars": 30, "max_chars": 1500,
                 "reservoir_cap": 250, "sample": [_sample_item("c1", "x")]}
        self.assertTrue(ep.state_sample_matches(
            state, limit=10, seed=1, min_chars=30, max_chars=1500,
            reservoir_cap=250))
        self.assertFalse(ep.state_sample_matches(
            state, limit=11, seed=1, min_chars=30, max_chars=1500,
            reservoir_cap=250))
        self.assertFalse(ep.state_sample_matches(
            {"sample": []}, limit=10, seed=1, min_chars=30, max_chars=1500,
            reservoir_cap=250))

    def test_filter_pending_skips_done(self):
        items = [_sample_item("c1", "a"), _sample_item("c2", "b"),
                 _sample_item("c3", "c")]
        pending, skipped = ep.filter_pending(items, {"c1", "c3"})
        self.assertEqual([it["chunk_id"] for it in pending], ["c2"])
        self.assertEqual(skipped, 2)


# ================================================================ 单块抽取


class TestExtractOne(unittest.TestCase):

    def test_success(self):
        client = FakeClient(default=json.dumps(
            [_proposition()], ensure_ascii=False))
        result = ep.extract_one(client, _sample_item(
            "c1", "办理时限：20个工作日。", "timeLimit"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["propositions"][0]["predicate"], "办理时限")

    def test_empty_extraction(self):
        client = FakeClient(default="[]")
        result = ep.extract_one(client, _sample_item("c1", "无事实内容。"))
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["propositions"], [])

    def test_timeout_then_success_retried(self):
        client = FakeClient(script=[TimeoutError("slow"),
                                    json.dumps([_proposition()],
                                               ensure_ascii=False)])
        result = ep.extract_one(client, _sample_item("c1", "内容"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(client.calls), 2)

    def test_http_500_retried_then_failed(self):
        client = FakeClient(script=[_http_error(500), _http_error(502)])
        result = ep.extract_one(client, _sample_item("c1", "内容"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 2)

    def test_http_400_not_retried(self):
        client = FakeClient(script=[_http_error(400)])
        result = ep.extract_one(client, _sample_item("c1", "内容"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["attempts"], 1)

    def test_parse_error_keeps_raw_head(self):
        client = FakeClient(default="抱歉，这段文字无法抽取事实。" + "x" * 300)
        result = ep.extract_one(client, _sample_item("c1", "内容"))
        self.assertEqual(result["status"], "parse_error")
        self.assertEqual(len(result["raw_head"]), 200)
        self.assertIn("抱歉", result["raw_head"])


# ================================================================ 并发归集


class TestRunExtractionOrdering(unittest.TestCase):

    def test_results_keyed_by_chunk_id_regardless_of_completion_order(self):
        items = [_sample_item(f"c{i:02d}", f"办理时限：{i}个工作日。", "timeLimit")
                 for i in range(9)]

        def fake_extract_one(client, item, *, retries=1):
            return {"status": "ok",
                    "propositions": [{"predicate": "办理时限",
                                      "condition_type": "",
                                      "object_value": "",
                                      "statement": f"{item['chunk_id']} 的陈述句"}],
                    "norm": {}, "attempts": 1}

        pool_holder: dict[str, FakePool] = {}

        def factory(workers):
            pool = FakePool(workers)
            pool_holder["pool"] = pool
            return pool

        with mock.patch.object(ep, "extract_one", fake_extract_one), \
                mock.patch.object(ep, "as_completed", _reversed_as_completed):
            results, _ = ep.run_extraction(
                items, FakeClient(default="[]"), workers=32, retries=1,
                log=lambda *_: None, executor_factory=factory)
        # 归集不依赖完成顺序：按 chunk_id 可取到各自结果
        self.assertEqual(set(results), {it["chunk_id"] for it in items})
        for it in items:
            self.assertEqual(
                results[it["chunk_id"]]["propositions"][0]["statement"],
                f"{it['chunk_id']} 的陈述句")
        # submit 次数 == 块数（每块恰好一次）
        self.assertEqual(len(pool_holder["pool"].futures), len(items))

    def test_collect_rows_follows_sample_order(self):
        items = [_sample_item(f"c{i:02d}", "内容", "process") for i in range(5)]
        results = {}
        for it in reversed(items):  # 构造乱序的结果字典
            results[it["chunk_id"]] = {
                "status": "ok",
                "propositions": [{"predicate": "其他", "condition_type": "",
                                  "object_value": "", "statement":
                                  f"{it['chunk_id']} 的陈述句内容"}],
                "norm": {}, "attempts": 1}
        rows = ep.collect_rows(items, results)
        self.assertEqual([r[0] for r in rows], [it["chunk_id"] for it in items])
        # parse_error / failed / 缺失结果的块不写行
        results["c00"] = {"status": "parse_error", "propositions": []}
        results.pop("c01")
        rows2 = ep.collect_rows(items, results)
        self.assertEqual({r[0] for r in rows2}, {"c02", "c03", "c04"})

    def test_real_threads_all_blocks_covered(self):
        # 真线程池小规模冒烟：并发下无丢失、无串块
        items = [_sample_item(f"t{i:02d}", f"办理条件：符合第{i}条规定情形的。",
                              "condition") for i in range(24)]

        def content_for(messages):
            title = messages[1]["content"].splitlines()[0]
            return json.dumps(
                [_proposition(statement=f"{title} 的办理条件成立，需提交材料。",
                              condition_type="资格要求")], ensure_ascii=False)

        client = FakeClient()
        client.chat = lambda messages: content_for(messages)
        results, _ = ep.run_extraction(items, client, workers=8, retries=1,
                                       log=lambda *_: None)
        self.assertEqual(len(results), 24)
        for it in items:
            result = results[it["chunk_id"]]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["propositions"][0]["condition_type"],
                             "资格要求")


# ================================================================ main 端到端


class TestMainEndToEnd(unittest.TestCase):

    def _run(self, tmp: Path, client: FakeClient, extra: list[str] | None = None,
             chunks_name: str = "chunks.csv"):
        chunks = tmp / chunks_name
        rows = [
            _chunk_row("c1", _FIELD_TEXTS["condition"]),
            _chunk_row("c2", _FIELD_TEXTS["process"]),
            _chunk_row("c3", _FIELD_TEXTS["material"]),
        ]
        _write_chunks_csv(chunks, rows)
        argv = [
            "--chunks", str(chunks),
            "--out", str(tmp / "props.csv"),
            "--summary", str(tmp / "summary.md"),
            "--state-file", str(tmp / "state.json"),
            "--limit", "3", "--seed", "11", "--workers", "4",
        ] + (extra or [])
        logs: list[str] = []
        rc = ep.main(argv, client_factory=lambda **kw: client, log=logs.append)
        return rc, logs

    def test_full_run_then_rerun_skips_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rc, logs = self._run(tmp, RespondingClient())
            self.assertEqual(rc, 0)
            out = tmp / "props.csv"
            state = ep.load_state(tmp / "state.json")
            # 3 块全部完成；样本顺序按 (source_field, category, chunk_id) 排序：
            # c1=condition, c3=material, c2=process
            self.assertEqual(len(state["done"]), 3)
            with out.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["chunk_id"] for r in rows],
                             ["c1", "c3", "c2"])
            self.assertEqual(rows[0]["condition_type"], "资格要求")
            self.assertEqual(rows[1]["condition_type"], "")
            self.assertEqual(rows[2]["condition_type"], "")
            self.assertEqual(
                list(rows[0].keys()),
                ["chunk_id", "service_id", "doc_id", "source_field",
                 "predicate", "condition_type", "object_value", "statement"])
            summary = (tmp / "summary.md").read_text(encoding="utf-8")
            self.assertTrue(summary.startswith("# 命题抽取试点报告"))
            self.assertIn("谓词分布", summary)
            self.assertIn("人工抽检清单", summary)
            # 第二次运行：state 复用样本，全部跳过，不追加行
            rc2, logs2 = self._run(tmp, RespondingClient())
            self.assertEqual(rc2, 0)
            self.assertTrue(any("跳过已完成 3 块" in m for m in logs2))
            with out.open(encoding="utf-8-sig", newline="") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 3)

    def test_parse_error_not_recorded_done_and_retried_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # 首跑：全部返回坏输出 → parse_error，不入 done、不写行
            bad = FakeClient(default="抱歉，无法抽取。" + "y" * 300)
            rc, logs = self._run(tmp, bad)
            self.assertEqual(rc, 0)
            self.assertTrue(any("parse_error" in m for m in logs))
            state = ep.load_state(tmp / "state.json")
            self.assertEqual(state["done"], [])
            with (tmp / "props.csv").open(encoding="utf-8-sig", newline="") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 0)  # 仅列头
            # 二跑：正常 client → 3 块补齐
            rc2, _ = self._run(tmp, RespondingClient())
            self.assertEqual(rc2, 0)
            state2 = ep.load_state(tmp / "state.json")
            self.assertEqual(len(state2["done"]), 3)
            with (tmp / "props.csv").open(encoding="utf-8-sig", newline="") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 3)

    def test_sample_only_writes_summary_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            client = RespondingClient()
            rc, _ = self._run(tmp, client, extra=["--sample-only"])
            self.assertEqual(rc, 0)
            self.assertEqual(client.calls, [])  # 未调用 LLM
            self.assertFalse((tmp / "props.csv").exists())
            text = (tmp / "summary.md").read_text(encoding="utf-8")
            self.assertIn("source_field", text)
            self.assertIn("人工抽检清单", text)

    def test_state_sample_reuse_skips_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rc, _ = self._run(tmp, RespondingClient())
            self.assertEqual(rc, 0)
            # 指向一个不存在的 chunks 文件：参数一致时应复用 state 样本，
            # 根本不读该文件
            other = tmp / "nonexistent.csv"
            argv = ["--chunks", str(other),
                    "--out", str(tmp / "props.csv"),
                    "--summary", str(tmp / "summary.md"),
                    "--state-file", str(tmp / "state.json"),
                    "--limit", "3", "--seed", "11"]
            logs: list[str] = []
            rc2 = ep.main(argv, client_factory=lambda **kw: RespondingClient(),
                          log=logs.append)
            self.assertEqual(rc2, 0)
            self.assertTrue(any("复用 state 缓存样本" in m for m in logs))
            self.assertFalse(other.exists())


if __name__ == "__main__":
    unittest.main()
