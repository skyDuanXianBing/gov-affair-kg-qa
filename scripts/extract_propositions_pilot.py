#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命题抽取试点：从 documents_chunks.csv 分层抽样 1000 块，调内网 LLM 抽取
"上下文独立命题"（SynthKG + SocraticKG 路线，docs/论文启示与改进路线.md 第 2 步、
docs/创建schema标准化流程.md 第 10 步）。

产出（不导入图，仅试点验证 prompt 质量 / 抽取密度 / 成本）：
  --out     data/pilot/propositions_pilot.csv
            列 chunk_id, service_id, doc_id, source_field, predicate,
               condition_type, object_value, statement
  --summary data/pilot/propositions_pilot_summary.md
            块统计 / 谓词分布 / conditionType 分布 / 分层密度 / 速度 / 人工抽检 20 条
  --state-file kg/import/checkpoints/extract_pilot_state.json
            断点续跑：记录已完成 chunk_id（ok/empty）；重跑跳过并复用缓存样本，
            免去对 17GB chunks 文件的重复全量扫描。

实测数据说明（与早期假设的差异，以数据为准）：
  1. documents_chunks.csv 实际列为 chunk_id, doc_id, chunk_no, chunk_count, title,
     content, category_l1, category_l2, service_id, department_name, source_url,
     source_file, source_line, extras_json——没有 source_field 列，但 category_l1
     直接在行内（分层不需要关联 services.csv）。
  2. source_field 由内容启发式派生：按块内出现的段落标签（"办理条件：" "办理流程："
     "申请材料：" 等）映射为 condition/process/material/... 伪字段（见 FIELD_RULES），
     多标签时按 FIELD_PRIORITY 取优先级最高者（受理条件优先，契合试点目标）。
  3. 文件按类别排序（前 3 万行全是"法人服务"），抽样必须全文件流式扫描，
     不可只读文件头部。

LLM 服务（OpenAI 兼容，内网）：
  POST {EXTRACT_BASE_URL}/chat/completions，模型 EXTRACT_MODEL（默认 qwen3.8-27b，
  思考型：只读取 message.content，忽略 reasoning_content）。实测支持 32 并发。
  EXTRACT_API_KEY 设置则追加 Authorization: Bearer（默认空，服务端不校验）。

重要：Git Bash 命令行传中文 JSON 会编码损坏，请求体一律在代码内用
json.dumps(...).encode("utf-8") 构造（与 scripts/backfill_vectors.py 同法）。

用法：
  python scripts/extract_propositions_pilot.py                 # 1000 块试点
  python scripts/extract_propositions_pilot.py --limit 50      # 小规模试跑
  python scripts/extract_propositions_pilot.py --sample-only   # 只抽样看分层，不调 LLM

思考链开/关 A/B 对比（默认关思考，与历史行为一致）：
  python scripts/extract_propositions_pilot.py --enable-thinking --max-tokens 16000 \
      --out data/pilot/propositions_pilot_thinking_on.csv \
      --summary data/pilot/propositions_pilot_thinking_on_summary.md \
      --state-file kg/import/checkpoints/extract_pilot_state_thinking_on.json
  注意：A/B 两组必须用不同前缀的 --out/--summary 与不同 --state-file（state 的
  done 记账按 chunk_id 跳过，共用会把另一组整体跳过）；开思考时 reasoning_content
  与 content 共享 completion 预算，--max-tokens 必须 enough 大（实测建议 16000，
  2000 量级会被思考链耗光导致 content 为 null）。两组结果用
  scripts/compare_thinking_ab.py 生成对比报告。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- LLM 服务配置

DEFAULT_BASE_URL = "http://10.130.71.10:30799/v1"
DEFAULT_MODEL = "qwen3.8-27b"
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = 6000
DEFAULT_TIMEOUT = 120.0
DEFAULT_WORKERS = 32

# ---------------------------------------------------------------- 受控词表

#: 谓词受控集合（docs/知识建模方案-v0.3-修订版.md §3.3 / 任务规范 §6.2）
CONTROLLED_PREDICATES: tuple[str, ...] = (
    "适用对象", "资格要求", "数量限制", "时间限制", "禁止情形", "例外情形",
    "材料要求", "办理地点", "收费标准", "办理时限", "办理流程", "其他",
)
OTHER_PREDICATE = "其他"

#: 条件类来源额外给出的粗粒度 conditionType（ServiceCondition.conditionType）
CONDITION_TYPES: tuple[str, ...] = (
    "资格要求", "数量限制", "时间限制", "禁止情形", "例外情形", "适用对象",
)

#: statement 最短长度（去空白后字符数），低于视为无效命题
MIN_STATEMENT_CHARS = 10

PROMPT_VERSION = "pilot-v1"

# ---------------------------------------------------------------- Prompt 常量

SYSTEM_PROMPT = """你是一名政务知识抽取器。输入是某个政务服务事项的一段原文（已知来源字段类型），你的任务是把它改写并抽取为"上下文独立命题"列表。

抽取要求：
1. 去语境化：每条命题的 statement 必须包含事项全称，禁止使用"本机关""该事项""申请人""上述材料"等依赖上下文的指代词；命题脱离原文后必须仍可独立理解。
2. 只输出一个纯 JSON 数组，不要用 markdown 代码块围栏，不要输出任何解释、前言或总结文字。数组元素结构：
   {"predicate": "<受控谓词>", "objectValue": "<值或事实>", "statement": "<完整独立陈述句>"}
3. predicate 必须严格从以下受控谓词集合中选择，禁止自造谓词：
   适用对象、资格要求、数量限制、时间限制、禁止情形、例外情形、材料要求、办理地点、收费标准、办理时限、办理流程、其他
4. statement 为一句完整的陈述句；objectValue 是命题的核心取值（金额、时限、份数、地点、情形描述等），金额/时限/数字一律保留原文表述，不要换算、不要补全、不要编造。
5. 一段原文可产出多条命题；同一事实不要重复输出；原文没有可抽取的事实时输出 []。
6. 命题只陈述原文明确表达的事实，禁止推测、禁止补充常识。"""

SYSTEM_PROMPT_CONDITION = SYSTEM_PROMPT + """

本任务的来源字段为"受理条件/办理条件"类。除上述要求外，每条命题额外携带一个字段 conditionType，取值必须从以下六类中恰好选择一个：
   资格要求、数量限制、时间限制、禁止情形、例外情形、适用对象
即数组元素结构为：
   {"predicate": "<受控谓词>", "conditionType": "<六类之一>", "objectValue": "<值或事实>", "statement": "<完整独立陈述句>"}"""

#: 派生 source_field → 用户消息里展示的中文字段名
SOURCE_FIELD_LABELS = {
    "condition": "受理条件/办理条件",
    "process": "办理流程（窗口办理/网上办理等）",
    "material": "申请材料",
    "fee": "收费标准",
    "timeLimit": "办理时限",
    "legalBasis": "设定依据/政策依据",
    "location": "办理地点",
    "supervision": "咨询监督",
    "faq": "常见问题",
    "header": "事项概览（事项名称起始段）",
    "other": "其他段落",
}

#: 段落标签 → 派生 source_field（子串匹配，标签后通常跟全角冒号）
FIELD_RULES: dict[str, tuple[str, ...]] = {
    "condition": ("受理条件", "办理条件", "申请条件", "准予条件", "审批条件"),
    "process": ("办理流程", "办理程序", "窗口办理", "网上办理", "办理方式"),
    "material": ("申请材料", "申报材料", "材料清单", "材料要求"),
    "fee": ("收费标准", "收费依据", "收费情况"),
    "timeLimit": ("办理时限", "法定时限", "承诺时限"),
    "legalBasis": ("设定依据", "法律依据", "法规依据", "政策依据"),
    "location": ("办理地点",),
    "supervision": ("监督投诉", "投诉举报", "咨询方式", "咨询电话"),
    "faq": ("常见问题",),
    "header": ("事项名称",),
}

#: 多标签同时命中时的归类优先级（受理条件优先，契合试点目标；未命中 → other）
FIELD_PRIORITY: tuple[str, ...] = (
    "condition", "process", "material", "fee", "timeLimit",
    "legalBasis", "location", "supervision", "faq", "header",
)

_LABEL_RE = re.compile("|".join(
    label for labels in FIELD_RULES.values() for label in labels
))
_LABEL_TO_FIELD = {label: field for field, labels in FIELD_RULES.items()
                   for label in labels}

# ---------------------------------------------------------------- 默认路径

DEFAULT_CHUNKS = "data/pilot/documents_chunks.csv"
DEFAULT_OUT = "data/pilot/propositions_pilot.csv"
DEFAULT_SUMMARY = "data/pilot/propositions_pilot_summary.md"
DEFAULT_STATE = "kg/import/checkpoints/extract_pilot_state.json"


def _default_log(msg: str) -> None:
    print(msg, flush=True)


# ================================================================ 抽样

def derive_source_field(content: str) -> str:
    """按内容中的段落标签启发式派生伪 source_field。

    只扫描传入文本（调用方传截断后的 content，与送入 LLM 的输入一致）。
    多标签命中时按 FIELD_PRIORITY 取最高优先级；未命中返回 "other"。
    """
    if not content:
        return "other"
    hits = {_LABEL_TO_FIELD[m] for m in _LABEL_RE.findall(content)}
    for field in FIELD_PRIORITY:
        if field in hits:
            return field
    return "other"


def is_condition_source(source_field: str) -> bool:
    """受理条件类来源（派生字段 condition）才要求命题携带 conditionType。"""
    return source_field == "condition"


def iter_chunk_rows(path: str | Path) -> Iterator[dict[str, str]]:
    """流式逐行读取 chunks CSV（utf-8-sig 容忍 BOM），不整表载入内存。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"chunks 文件不存在：{p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        required = ("chunk_id", "content")
        missing = [c for c in required if c not in fieldnames]
        if missing:
            raise ValueError(f"{p}: 缺少必需列 {missing}，实际列 {fieldnames}")
        yield from reader


def _sample_item(row: dict[str, str], content: str, source_field: str,
                 category: str, max_chars: int) -> dict[str, str]:
    return {
        "chunk_id": (row.get("chunk_id") or "").strip(),
        "doc_id": (row.get("doc_id") or "").strip(),
        "title": (row.get("title") or "").strip(),
        "service_id": (row.get("service_id") or "").strip(),
        "category_l1": category,
        "source_field": source_field,
        "chunk_no": (row.get("chunk_no") or "").strip(),
        # 送入 LLM 的文本就是截断后的 content；state 缓存样本同样只存截断文本
        "content": content[:max_chars],
    }


def scan_and_sample(
    rows_iter: Iterable[dict[str, str]],
    *,
    limit: int,
    seed: int,
    min_chars: int,
    max_chars: int,
    reservoir_cap: int,
    log: Callable[[str], None] = _default_log,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """单遍流式扫描 + 分层蓄水池抽样。

    分层键 = 派生 source_field × category_l1。每层维护容量 reservoir_cap 的
    蓄水池（经典 Algorithm R，seeded），扫描结束后按各层总行数比例分配 limit
    个名额（每层至少 1 个、不超过蓄水池存量），层内用同一 rng 抽取，保证
    同参数同 seed 可复现。返回 (样本列表, 统计)。

    内存上界 ≈ 非空层数 × reservoir_cap × (max_chars + 元数据)，content 只存
    截断后文本，因此 17GB 输入也不会整表进内存。
    """
    rng = random.Random(seed)
    reservoirs: dict[str, list[dict[str, str]]] = {}
    totals: dict[str, int] = {}
    stats = {"scanned": 0, "skipped_short": 0, "skipped_no_id": 0}
    for row in rows_iter:
        stats["scanned"] += 1
        if stats["scanned"] % 500_000 == 0:
            log(f"[扫描] 已读 {stats['scanned']} 行，发现 {len(totals)} 个分层")
        content = (row.get("content") or "").strip()
        if len(content) < min_chars:
            stats["skipped_short"] += 1
            continue
        if not (row.get("chunk_id") or "").strip():
            stats["skipped_no_id"] += 1
            continue
        truncated = content[:max_chars]
        field = derive_source_field(truncated)
        category = (row.get("category_l1") or "").strip() or "未知"
        stratum = f"{field}|{category}"
        totals[stratum] = totals.get(stratum, 0) + 1
        item = _sample_item(row, truncated, field, category, max_chars)
        res = reservoirs.setdefault(stratum, [])
        if len(res) < reservoir_cap:
            res.append(item)
        else:
            # Algorithm R：该层第 n 条以 reservoir_cap/n 的概率随机替换
            j = rng.randrange(totals[stratum])
            if j < reservoir_cap:
                res[j] = item
    sample = _allocate_and_pick(reservoirs, totals, limit, rng)
    stats["strata"] = len(totals)
    stats["candidates"] = sum(len(v) for v in reservoirs.values())
    stats["sampled"] = len(sample)
    return sample, stats


def _allocate_and_pick(
    reservoirs: dict[str, list[dict[str, str]]],
    totals: dict[str, int],
    limit: int,
    rng: random.Random,
) -> list[dict[str, str]]:
    """按层总行数比例分配 limit 个名额并在层内抽取（确定性归总排序）。"""
    strata = [s for s in sorted(reservoirs) if reservoirs[s]]
    if not strata or limit <= 0:
        return []
    available = sum(len(reservoirs[s]) for s in strata)
    if available <= limit:
        quota = {s: len(reservoirs[s]) for s in strata}
    else:
        grand = sum(totals[s] for s in strata)
        quota = {
            s: max(1, min(len(reservoirs[s]),
                          int(round(limit * totals[s] / grand))))
            for s in strata
        }
        quota = _fit_quota(quota, {s: len(reservoirs[s]) for s in strata},
                           limit, totals)
    picked: list[dict[str, str]] = []
    for s in strata:
        q = quota.get(s, 0)
        if q > 0:
            picked.extend(rng.sample(reservoirs[s], q))
    # 输出顺序确定：与完成顺序、层遍历顺序无关
    picked.sort(key=lambda it: (it["source_field"], it["category_l1"],
                                it["chunk_id"]))
    return picked


def _fit_quota(quota: dict[str, int], caps: dict[str, int], limit: int,
               totals: dict[str, int]) -> dict[str, int]:
    """把各层名额调整到总和恰好等于 limit（超减不足补，确定性无随机）。"""
    quota = dict(quota)
    active = [s for s in quota if quota[s] > 0]
    guard = 0
    while sum(quota.values()) != limit and guard < 1_000_000:
        guard += 1
        if sum(quota.values()) > limit:
            s = max(active, key=lambda x: (quota[x], totals.get(x, 0)))
            if quota[s] > 1:
                quota[s] -= 1
            else:
                # 所有层都只剩 1 且仍超限：去掉总量最小的整层
                s2 = min(active, key=lambda x: (totals.get(x, 0), x))
                quota[s2] = 0
                active = [x for x in active if x != s2]
                if not active:
                    break
        else:
            candidates = [s for s in active if quota[s] < caps[s]]
            if not candidates:
                break
            s = max(candidates, key=lambda x: (totals.get(x, 0), x))
            quota[s] += 1
    return {s: q for s, q in quota.items() if q > 0}


# ================================================================ LLM 客户端

class LlmError(Exception):
    """LLM 调用失败（网络/HTTP/响应结构）。"""

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


def _is_retriable(exc: BaseException) -> bool:
    """超时、连接层错误、5xx 与 429 可重试；4xx 业务错误不重试。"""
    if isinstance(exc, LlmError):
        return exc.retriable
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError,
                        ConnectionError, OSError)):
        return True
    return False


class LlmClient:
    """OpenAI 兼容 Chat Completions 客户端（urllib，无第三方依赖）。

    qwen3.8-27b 为思考型模型：响应 message.reasoning_content 是思考链，
    本类只返回 message.content（最终答案）。

    enable_thinking=False（默认，向后兼容）：请求体显式带
    chat_template_kwargs={"enable_thinking": False} 关闭思考链。
    enable_thinking=True：请求体不带 chat_template_kwargs（qwen3.8 默认开思考），
    此时 reasoning_content 与 content 共享 completion 预算，必须配大 max_tokens
    （实测建议 16000；2000 量级会被思考链耗光导致 content 为 null）。
    """

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                 *, enable_thinking: bool = False,
                 max_tokens: int = LLM_MAX_TOKENS):
        self.endpoint = (base_url or os.environ.get(
            "EXTRACT_BASE_URL", DEFAULT_BASE_URL)).rstrip("/") + "/chat/completions"
        self.model = model or os.environ.get("EXTRACT_MODEL", DEFAULT_MODEL)
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("EXTRACT_API_KEY", ""))
        self.timeout = timeout
        self.enable_thinking = enable_thinking
        self.max_tokens = max_tokens

    def chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": self.max_tokens,
        }
        if not self.enable_thinking:
            # 思考链会把 completion 预算耗光导致 content 为 null（实测）；
            # qwen3 系列支持关闭思考，抽取任务无需思考链，且更快。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(200).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 —— 读错误详情失败不影响归类
                pass
            retriable = e.code >= 500 or e.code == 429
            raise LlmError(
                f"LLM 返回 HTTP {e.code}（{self.endpoint}, model={self.model}）："
                f"{detail}", retriable=retriable) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError) as e:
            raise LlmError(f"LLM 连接失败（{self.endpoint}）：{e!r}",
                           retriable=True) from e
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise LlmError(
                f"LLM 响应不是合法 JSON（{self.endpoint}）：{raw[:200]!r}",
                retriable=True) from e
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LlmError(f"LLM 响应缺少 choices：{str(payload)[:200]}",
                           retriable=True)
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LlmError("LLM 响应缺少字符串 message.content"
                           "（思考型模型只取 content，忽略 reasoning_content）",
                           retriable=True)
        return content


def build_messages(item: dict[str, str]) -> list[dict[str, str]]:
    """构造单块请求消息：条件类来源用带 conditionType 指令的系统提示。"""
    if is_condition_source(item["source_field"]):
        system = SYSTEM_PROMPT_CONDITION
    else:
        system = SYSTEM_PROMPT
    title = item["title"] or item["service_id"] or item["doc_id"] or "未知事项"
    field_label = SOURCE_FIELD_LABELS.get(item["source_field"], item["source_field"])
    user = (f"事项名称：{title}\n"
            f"来源字段类型：{field_label}\n"
            f"原文：\n{item['content']}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ================================================================ 解析与归一

def parse_json_array(text: str) -> list:
    """从模型输出解析 JSON 数组：剥围栏 → 取首个 [ 到末个 ] → json.loads。

    解析失败抛 ValueError（调用方记 parse_error）。
    """
    if not isinstance(text, str):
        raise ValueError(f"输出不是字符串：{type(text).__name__}")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"输出中未找到 JSON 数组：{stripped[:120]!r}")
    snippet = stripped[start:end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败：{e}；片段 {snippet[:120]!r}") from e
    if not isinstance(data, list):
        raise ValueError("JSON 顶层不是数组")
    return data


def normalize_propositions(
    raw_list: list,
    source_field: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """校验并归一模型输出的命题列表。

    - predicate 不在受控集合（含空/非字符串）→ 归"其他"并计数；
    - statement 去空白后为空或不足 MIN_STATEMENT_CHARS 字 → 丢弃计数；
    - statement 完全相同的重复命题只保留第一条；
    - conditionType 仅条件类来源保留（且必须六选一，非法置空计数）；
      非条件类来源一律删除该字段。
    返回 (命题列表, 计数统计)。
    """
    stats = {
        "raw": len(raw_list),
        "invalid_predicate": 0,
        "bad_statement": 0,
        "duplicate": 0,
        "invalid_condition_type": 0,
    }
    condition_source = is_condition_source(source_field)
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            stats["bad_statement"] += 1
            continue
        predicate = raw.get("predicate")
        if isinstance(predicate, str):
            predicate = predicate.strip()
        else:
            predicate = ""
        if predicate not in CONTROLLED_PREDICATES:
            stats["invalid_predicate"] += 1
            predicate = OTHER_PREDICATE
        statement = raw.get("statement")
        if isinstance(statement, str):
            statement = statement.strip()
        else:
            statement = ""
        if len(statement) < MIN_STATEMENT_CHARS:
            stats["bad_statement"] += 1
            continue
        if statement in seen:
            stats["duplicate"] += 1
            continue
        seen.add(statement)
        object_value = raw.get("objectValue")
        if not isinstance(object_value, str):
            object_value = ""
        condition_type = ""
        if condition_source:
            ct = raw.get("conditionType")
            if isinstance(ct, str):
                ct = ct.strip()
                if ct in CONDITION_TYPES:
                    condition_type = ct
                elif ct:
                    stats["invalid_condition_type"] += 1
        out.append({
            "predicate": predicate,
            "condition_type": condition_type,
            "object_value": object_value.strip(),
            "statement": statement,
        })
    stats["kept"] = len(out)
    return out, stats


def extract_one(client: LlmClient, item: dict[str, str], *,
                retries: int = 1) -> dict:
    """单块抽取：调 LLM（超时/5xx/429/连接错误重试 retries 次）→ 解析 → 归一。

    返回 status ∈ {ok, empty, parse_error, failed}；parse_error 附原始输出
    前 200 字（raw_head）供日志排查；failed 附 error 文本。不抛异常。
    """
    messages = build_messages(item)
    content = ""
    last_error = ""
    for attempt in range(retries + 1):
        try:
            content = client.chat(messages)
            break
        except Exception as e:  # noqa: BLE001 —— 单块失败不中断整批
            last_error = f"{type(e).__name__}: {e}"
            if not _is_retriable(e) or attempt == retries:
                return {"status": "failed", "error": last_error,
                        "propositions": [], "norm": {}, "attempts": attempt + 1}
    try:
        raw_list = parse_json_array(content)
    except ValueError as e:
        return {"status": "parse_error", "error": str(e),
                "raw_head": content[:200], "propositions": [], "norm": {},
                "attempts": retries + 1}
    propositions, norm_stats = normalize_propositions(raw_list,
                                                      item["source_field"])
    status = "ok" if propositions else "empty"
    return {"status": status, "error": "", "propositions": propositions,
            "norm": norm_stats, "attempts": retries + 1}


def run_extraction(
    items: Sequence[dict[str, str]],
    client: LlmClient,
    *,
    workers: int,
    retries: int,
    log: Callable[[str], None] = _default_log,
    executor_factory: Callable[[int], ThreadPoolExecutor] | None = None,
) -> tuple[dict[str, dict], float]:
    """并发抽取全部块；返回 ({chunk_id: 结果}, 耗时秒)。

    结果按 chunk_id 键归集，与 future 完成顺序无关；executor_factory 供
    单测注入乱序完成的假线程池。
    """
    results: dict[str, dict] = {}
    total = len(items)
    if total == 0:
        return results, 0.0
    start = time.time()
    factory = executor_factory or (lambda w: ThreadPoolExecutor(max_workers=w))
    with factory(workers) as pool:
        future_map = {pool.submit(extract_one, client, item, retries=retries):
                      item["chunk_id"] for item in items}
        done = 0
        for future in as_completed(future_map):
            chunk_id = future_map[future]
            try:
                results[chunk_id] = future.result()
            except Exception as e:  # noqa: BLE001 —— 防御：extract_one 不应抛
                results[chunk_id] = {"status": "failed",
                                     "error": f"{type(e).__name__}: {e}",
                                     "propositions": [], "norm": {},
                                     "attempts": 0}
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = max(time.time() - start, 1e-6)
                log(f"[抽取 {done}/{total}] {done / elapsed:.1f} 块/秒 "
                    f"(ok={sum(1 for r in results.values() if r['status'] == 'ok')} "
                    f"empty={sum(1 for r in results.values() if r['status'] == 'empty')} "
                    f"parse_error={sum(1 for r in results.values() if r['status'] == 'parse_error')} "
                    f"failed={sum(1 for r in results.values() if r['status'] == 'failed')})")
    return results, time.time() - start


# ================================================================ 断点 state

def load_state(path: str | Path) -> dict:
    """读取断点 state（坏文件返回空 state 并告警，视作从头开始）。"""
    p = Path(path)
    if not p.is_file():
        return {"done": []}
    try:
        state = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(state, dict) and isinstance(state.get("done"), list):
            return state
        raise ValueError("state 顶层结构不对")
    except (OSError, ValueError) as e:
        print(f"警告: 读取 state 失败（{p}）：{e!r}，视作无断点重新开始",
              file=sys.stderr)
        return {"done": []}


def save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def state_sample_matches(state: dict, *, limit: int, seed: int,
                         min_chars: int, max_chars: int,
                         reservoir_cap: int) -> bool:
    """state 中缓存的样本是否与当前抽样参数一致（一致可免重扫 17GB）。"""
    sample = state.get("sample")
    if not isinstance(sample, list) or not sample:
        return False
    keys = {"limit": limit, "seed": seed, "min_chars": min_chars,
            "max_chars": max_chars, "reservoir_cap": reservoir_cap}
    return all(state.get(k) == v for k, v in keys.items())


def filter_pending(items: Sequence[dict[str, str]],
                   done_ids: set[str]) -> tuple[list[dict[str, str]], int]:
    """从样本中剔除已完成块。返回 (待跑列表, 跳过数)。"""
    pending = [it for it in items if it["chunk_id"] not in done_ids]
    return pending, len(items) - len(pending)


# ================================================================ 输出

OUT_COLUMNS = ["chunk_id", "service_id", "doc_id", "source_field", "predicate",
               "condition_type", "object_value", "statement"]


def append_rows(path: str | Path, rows: list[list[str]]) -> None:
    """追加写结果 CSV（utf-8-sig；文件不存在时写列头）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    need_header = not p.is_file() or p.stat().st_size == 0
    with p.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if need_header:
            writer.writerow(OUT_COLUMNS)
        writer.writerows(rows)
        fh.flush()


def collect_rows(items: Sequence[dict[str, str]],
                 results: dict[str, dict]) -> list[list[str]]:
    """按样本顺序（非完成顺序）把结果展开为输出行。"""
    rows: list[list[str]] = []
    for item in items:
        result = results.get(item["chunk_id"])
        if not result or result.get("status") not in ("ok", "empty"):
            continue
        for prop in result.get("propositions", []):
            rows.append([
                item["chunk_id"], item["service_id"], item["doc_id"],
                item["source_field"], prop["predicate"],
                prop["condition_type"], prop["object_value"], prop["statement"],
            ])
    return rows


def _md_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def write_summary(
    path: str | Path,
    *,
    client: LlmClient,
    sample: Sequence[dict[str, str]],
    pending: Sequence[dict[str, str]],
    results: dict[str, dict],
    elapsed: float,
    scan_stats: dict[str, int],
    args: argparse.Namespace,
    done_total: int,
    raw_samples: Sequence[dict[str, str]],
    log: Callable[[str], None] = _default_log,
    extract_ran: bool = True,
) -> None:
    """写 Markdown 汇总报告（覆盖写，反映本次运行）。"""
    by_status = {"ok": 0, "empty": 0, "parse_error": 0, "failed": 0}
    norm_total = {"raw": 0, "invalid_predicate": 0, "bad_statement": 0,
                  "duplicate": 0, "invalid_condition_type": 0, "kept": 0}
    predicate_dist = {p: 0 for p in CONTROLLED_PREDICATES}
    cond_dist = {c: 0 for c in CONDITION_TYPES}
    cond_dist["(未给出)"] = 0
    field_blocks: dict[str, int] = {}
    field_props: dict[str, int] = {}
    propositions_count = 0
    for item in pending:
        result = results.get(item["chunk_id"])
        if not result:
            continue
        status = result.get("status", "failed")
        if status in by_status:
            by_status[status] += 1
        field_blocks[item["source_field"]] = (
            field_blocks.get(item["source_field"], 0) + 1)
        for k, v in (result.get("norm") or {}).items():
            if k in norm_total:
                norm_total[k] += v
        for prop in result.get("propositions", []):
            propositions_count += 1
            predicate_dist[prop["predicate"]] = (
                predicate_dist.get(prop["predicate"], 0) + 1)
            field_props[item["source_field"]] = (
                field_props.get(item["source_field"], 0) + 1)
            if item["source_field"] == "condition":
                key = prop["condition_type"] if prop["condition_type"] else "(未给出)"
                cond_dist[key] = cond_dist.get(key, 0) + 1

    attempted = by_status["ok"] + by_status["empty"] + by_status["parse_error"] + by_status["failed"]
    extracted_blocks = by_status["ok"] + by_status["empty"]
    avg_per_block = (propositions_count / extracted_blocks
                     if extracted_blocks else 0.0)
    rate = attempted / elapsed if elapsed > 0 else 0.0

    sample_field_blocks: dict[str, int] = {}
    for item in sample:
        sample_field_blocks[item["source_field"]] = (
            sample_field_blocks.get(item["source_field"], 0) + 1)

    lines: list[str] = []
    lines.append("# 命题抽取试点报告（SynthKG + SocraticKG 路线）")
    lines.append("")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 模型：{client.model}（endpoint {client.endpoint}，"
                 f"temperature {LLM_TEMPERATURE}，"
                 f"max_tokens {args.max_tokens}，"
                 f"思考链：{'开' if args.enable_thinking else '关'}）")
    lines.append(f"- prompt 版本：{PROMPT_VERSION}")
    lines.append(f"- 参数：--limit {args.limit} --seed {args.seed} "
                 f"--workers {args.workers} --timeout {args.timeout} "
                 f"--max-chunk-chars {args.max_chunk_chars} "
                 f"--min-chunk-chars {args.min_chunk_chars} "
                 f"--reservoir-cap {args.reservoir_cap} "
                 f"--enable-thinking {args.enable_thinking} "
                 f"--max-tokens {args.max_tokens}")
    lines.append("")
    lines.append("## 块统计")
    lines.append("")
    lines.append(f"- 抽样块数：{len(sample)}（扫描 {scan_stats.get('scanned', 0)} 行，"
                 f"过滤短块 {scan_stats.get('skipped_short', 0)}，"
                 f"{scan_stats.get('strata', 0)} 个分层，候选 {scan_stats.get('candidates', 0)}）")
    if not extract_ran:
        lines.append("- 本次运行：--sample-only 模式，未调用 LLM（下述块/命题统计为 0 属预期）")
    else:
        lines.append(f"- 本次运行：{attempted} 块；断点跳过 {len(sample) - len(pending)} 块"
                     f"（state 累计完成 {done_total} 块）")
    lines.append(f"- 成功（有命题）：{by_status['ok']} 块")
    lines.append(f"- 空抽（无有效事实，输出 []）：{by_status['empty']} 块")
    lines.append(f"- 解析失败（parse_error）：{by_status['parse_error']} 块")
    lines.append(f"- 调用失败（failed，重试后仍失败）：{by_status['failed']} 块")
    lines.append("")
    lines.append("## 命题统计")
    lines.append("")
    lines.append(f"- 命题总数（本次输出行）：{propositions_count}")
    lines.append(f"- 每块平均命题数（按成功+空抽块计）：{avg_per_block:.2f}")
    lines.append(f"- 归一损耗：原始 {norm_total['raw']} 条 → 保留 {norm_total['kept']} 条"
                 f"（谓词归其他 {norm_total['invalid_predicate']}，"
                 f"statement 无效丢弃 {norm_total['bad_statement']}，"
                 f"重复去重 {norm_total['duplicate']}，"
                 f"conditionType 非法 {norm_total['invalid_condition_type']}）")
    lines.append("")
    lines.append("## 谓词分布")
    lines.append("")
    lines.append(_md_table(
        ["谓词", "数量", "占比"],
        [(p, predicate_dist.get(p, 0),
          f"{predicate_dist.get(p, 0) / propositions_count:.1%}"
          if propositions_count else "0.0%")
         for p in CONTROLLED_PREDICATES]))
    lines.append("")
    lines.append("## conditionType 分布（仅受理条件类来源命题）")
    lines.append("")
    lines.append(_md_table(
        ["conditionType", "数量"],
        [(c, cond_dist.get(c, 0)) for c in list(CONDITION_TYPES) + ["(未给出)"]]))
    lines.append("")
    lines.append("## 按派生 source_field 的抽取密度（本次运行）")
    lines.append("")
    density_rows = []
    for field in sorted(set(field_blocks) | set(sample_field_blocks)):
        ran = field_blocks.get(field, 0)
        props = field_props.get(field, 0)
        density_rows.append((field, sample_field_blocks.get(field, 0), ran,
                             props, f"{props / ran:.2f}" if ran else "-"))
    lines.append(_md_table(
        ["source_field（派生）", "抽样块数", "本次运行块数", "命题数", "命题/块"],
        density_rows))
    lines.append("")
    lines.append("## 速度与耗时")
    lines.append("")
    lines.append(f"- 抽取耗时：{elapsed:.1f} 秒（{rate:.1f} 块/秒，"
                 f"{args.workers} 并发）")
    lines.append("")
    lines.append("## 人工抽检清单（随机 20 条：statement 与原文对照）")
    lines.append("")
    rng = random.Random(args.seed ^ 0x5F17)
    pool = list(raw_samples)
    picks = rng.sample(pool, min(20, len(pool))) if pool else []
    if not picks:
        lines.append("（本次运行没有可抽检的命题）")
    for i, entry in enumerate(picks, 1):
        snippet = " ".join(entry["content"][:300].split())
        lines.append(f"{i}. [{entry['predicate']}] {entry['statement']}")
        lines.append(f"   - chunk：{entry['chunk_id']}"
                     f"（source_field={entry['source_field']}）")
        lines.append(f"   - 原文片段：{snippet}")
    lines.append("")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    log(f"[汇总] 报告已写入 {p}")


def build_raw_samples(items: Sequence[dict[str, str]],
                      results: dict[str, dict]) -> list[dict[str, str]]:
    """为人工抽检收集 (命题, 来源块) 对。"""
    out = []
    for item in items:
        result = results.get(item["chunk_id"])
        if not result or result.get("status") != "ok":
            continue
        for prop in result.get("propositions", []):
            out.append({
                "chunk_id": item["chunk_id"],
                "source_field": item["source_field"],
                "predicate": prop["predicate"],
                "statement": prop["statement"],
                "content": item["content"],
            })
    return out


# ================================================================ CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="extract_propositions_pilot",
        description="命题抽取试点：chunks 分层抽样 → 内网 LLM 抽取上下文独立命题"
                    "（不导入图）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="环境变量：EXTRACT_BASE_URL / EXTRACT_MODEL / EXTRACT_API_KEY"
               "（见 .env.example）。",
    )
    parser.add_argument("--chunks", metavar="CSV", default=DEFAULT_CHUNKS,
                        help=f"chunks 输入 CSV（默认 {DEFAULT_CHUNKS}，流式读取）")
    parser.add_argument("--out", metavar="CSV", default=DEFAULT_OUT,
                        help=f"命题输出 CSV（默认 {DEFAULT_OUT}，追加写）")
    parser.add_argument("--summary", metavar="MD", default=DEFAULT_SUMMARY,
                        help=f"汇总报告 Markdown（默认 {DEFAULT_SUMMARY}，覆盖写）")
    parser.add_argument("--state-file", metavar="JSON", default=DEFAULT_STATE,
                        help=f"断点 state（默认 {DEFAULT_STATE}）")
    parser.add_argument("--limit", type=int, default=1000, metavar="N",
                        help="抽样块数（默认 1000）")
    parser.add_argument("--seed", type=int, default=20260905, metavar="N",
                        help="抽样与抽检随机种子（默认 20260905）")
    parser.add_argument("--min-chunk-chars", type=int, default=30, metavar="N",
                        help="短块过滤阈值（默认 30 字符）")
    parser.add_argument("--max-chunk-chars", type=int, default=1500,
                        metavar="N",
                        help="送入 LLM 的文本截断上限（默认 1500 字符）")
    parser.add_argument("--reservoir-cap", type=int, default=250, metavar="N",
                        help="每分层蓄水池容量（默认 250，内存上界相关）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        metavar="N", help=f"并发数（默认 {DEFAULT_WORKERS}）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        metavar="SEC", help=f"单请求超时（默认 {DEFAULT_TIMEOUT}s）")
    parser.add_argument("--enable-thinking", action="store_true",
                        help="开启 LLM 思考链（默认关，与历史行为一致）。开思考时"
                             "请求体不带 chat_template_kwargs（qwen3.8 默认开思考），"
                             "且必须配大 --max-tokens（建议 16000），否则思考链会"
                             "耗光 completion 预算导致 content 为 null。A/B 对比时"
                             "请与关思考组使用不同前缀的 --out/--summary 与不同 "
                             "--state-file")
    parser.add_argument("--max-tokens", type=int, default=LLM_MAX_TOKENS,
                        metavar="N",
                        help=f"请求 max_tokens（默认 {LLM_MAX_TOKENS}；"
                             f"--enable-thinking 时建议 16000）")
    parser.add_argument("--retries", type=int, default=1, metavar="N",
                        help="单块重试次数（默认 1，即最多调用 1+1 次）")
    parser.add_argument("--sample-only", action="store_true",
                        help="只抽样并写汇总的分层统计，不调用 LLM")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit 必须 >= 1")
    if args.min_chunk_chars < 0:
        parser.error("--min-chunk-chars 必须 >= 0")
    if args.max_chunk_chars < args.min_chunk_chars:
        parser.error("--max-chunk-chars 必须 >= --min-chunk-chars")
    if args.reservoir_cap < 1:
        parser.error("--reservoir-cap 必须 >= 1")
    if args.workers < 1:
        parser.error("--workers 必须 >= 1")
    if args.timeout <= 0:
        parser.error("--timeout 必须 > 0")
    if args.max_tokens < 1:
        parser.error("--max-tokens 必须 >= 1")
    if args.retries < 0:
        parser.error("--retries 必须 >= 0")
    return args


def main(argv: list[str] | None = None,
         client_factory: Callable[..., LlmClient] | None = None,
         log: Callable[[str], None] = _default_log) -> int:
    args = parse_args(argv)
    client = (client_factory or LlmClient)(
        timeout=args.timeout, enable_thinking=args.enable_thinking,
        max_tokens=args.max_tokens)

    state = load_state(args.state_file)
    done_ids = {str(c).strip() for c in state.get("done", [])}
    if state.get("done"):
        mismatches: list[str] = []
        if state.get("model") != client.model:
            mismatches.append(f"model={state.get('model')!r} → {client.model!r}")
        if state.get("prompt_version") != PROMPT_VERSION:
            mismatches.append(
                f"prompt_version={state.get('prompt_version')!r} → "
                f"{PROMPT_VERSION!r}")
        # 思考模式不同的 state 混用（如 A/B 组误共用）只告警不阻断：
        # done 记账按 chunk_id 跳过，共用会把另一组整体跳过。
        if state.get("thinking") is not None and \
                state.get("thinking") != args.enable_thinking:
            mismatches.append(
                f"thinking={state.get('thinking')} → {args.enable_thinking}")
        if mismatches:
            print(f"警告: state 与当前运行不一致（{'；'.join(mismatches)}），"
                  f"继续按已完成 chunk_id 跳过；如需全新抽取请更换 "
                  f"--state-file。", file=sys.stderr)

    # ---- 抽样（可复用 state 缓存样本，免重扫 17GB 输入）
    if state_sample_matches(state, limit=args.limit, seed=args.seed,
                            min_chars=args.min_chunk_chars,
                            max_chars=args.max_chunk_chars,
                            reservoir_cap=args.reservoir_cap):
        sample = state["sample"]
        scan_stats = {"scanned": 0, "skipped_short": 0,
                      "strata": len({f"{it['source_field']}|{it['category_l1']}"
                                     for it in sample}),
                      "candidates": len(sample)}
        log(f"[抽样] 复用 state 缓存样本 {len(sample)} 块（跳过全文件扫描）")
    else:
        log(f"[抽样] 流式扫描 {args.chunks} 并分层抽样 "
            f"(limit={args.limit} seed={args.seed})")
        t0 = time.time()
        sample, scan_stats = scan_and_sample(
            iter_chunk_rows(args.chunks),
            limit=args.limit, seed=args.seed,
            min_chars=args.min_chunk_chars, max_chars=args.max_chunk_chars,
            reservoir_cap=args.reservoir_cap, log=log)
        log(f"[抽样] 扫描 {scan_stats['scanned']} 行耗时 "
            f"{time.time() - t0:.1f}s，命中 {scan_stats['strata']} 层，"
            f"抽样 {len(sample)} 块（候选 {scan_stats['candidates']}）")
        if not sample:
            print("[错误] 没有抽到任何块（检查输入文件与过滤参数）",
                  file=sys.stderr)
            return 2
        # 样本连同参数缓存进 state：中断重跑可免重复全量扫描
        state.update({
            "model": client.model, "prompt_version": PROMPT_VERSION,
            "limit": args.limit, "seed": args.seed,
            "min_chars": args.min_chunk_chars,
            "max_chars": args.max_chunk_chars,
            "reservoir_cap": args.reservoir_cap,
            "sample": sample,
            "done": sorted(done_ids),
        })
        save_state(args.state_file, state)

    pending, skipped = filter_pending(sample, done_ids)
    log(f"[断点] 样本 {len(sample)} 块，跳过已完成 {skipped} 块，"
        f"待运行 {len(pending)} 块")

    if args.sample_only or not pending:
        if args.sample_only:
            field_dist: dict[str, int] = {}
            for it in sample:
                field_dist[it["source_field"]] = field_dist.get(
                    it["source_field"], 0) + 1
            log("[sample-only] 派生 source_field 分布："
                + ", ".join(f"{k}={v}" for k, v in sorted(field_dist.items())))
            write_summary(args.summary, client=client, sample=sample,
                          pending=[], results={}, elapsed=0.0,
                          scan_stats=scan_stats, args=args,
                          done_total=len(done_ids), raw_samples=[], log=log,
                          extract_ran=False)
        else:
            log("[完成] 全部样本块已完成（如需重跑失败块，请更换 --state-file）")
        return 0

    # ---- 并发抽取
    log(f"[抽取] {len(pending)} 块，{args.workers} 并发，"
        f"model={client.model}，prompt={PROMPT_VERSION}")
    results, elapsed = run_extraction(
        pending, client, workers=args.workers, retries=args.retries, log=log)

    for chunk_id, result in sorted(results.items()):
        if result["status"] == "parse_error":
            log(f"[parse_error] {chunk_id}: {result.get('error', '')}；"
                f"原始输出前 200 字：{result.get('raw_head', '')!r}")
        elif result["status"] == "failed":
            log(f"[failed] {chunk_id}: {result.get('error', '')}")

    # ---- 输出（按样本顺序展开，与完成顺序无关）
    rows = collect_rows(pending, results)
    append_rows(args.out, rows)
    log(f"[输出] 新增命题 {len(rows)} 行 → {args.out}")

    # ---- 更新 state：ok/empty 记为完成（parse_error/failed 重跑时自动重试）
    for item in pending:
        result = results.get(item["chunk_id"])
        if result and result.get("status") in ("ok", "empty"):
            done_ids.add(item["chunk_id"])
    state.update({"model": client.model, "prompt_version": PROMPT_VERSION,
                  "thinking": args.enable_thinking,
                  "max_tokens": args.max_tokens,
                  "done": sorted(done_ids)})
    save_state(args.state_file, state)

    raw_samples = build_raw_samples(pending, results)
    write_summary(args.summary, client=client, sample=sample, pending=pending,
                  results=results, elapsed=elapsed, scan_stats=scan_stats,
                  args=args, done_total=len(done_ids), raw_samples=raw_samples,
                  log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
