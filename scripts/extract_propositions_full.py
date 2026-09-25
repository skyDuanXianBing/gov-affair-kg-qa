#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量命题抽取：documents_chunks.csv（约 130 万块）→ 内网 LLM（关思考）→ 命题 CSV。

与试点脚本 scripts/extract_propositions_pilot.py 的关系：prompt、消息构造、
JSON 解析、命题归一、LLM 客户端（默认关思考）全部 import 复用（pilot-v1，
保证与试点 B 组同分布），本脚本只做"全量 + 断点续跑 + 长跑"的工程包装。

与试点断点模式的差异（为什么不用试点的 state）：试点在运行末一次性全量重写
JSON state 并把全部结果攒在内存，131 万块下不可行。本脚本照抄
scripts/backfill_vectors.py BackfillState 的追加式模式：
  - state 是 JSON Lines（kg/import/checkpoints/extract_full_state.jsonl），
    每行 {"chunk_id": ..., "status": "ok"|"empty"}，完成一块追加一行；
  - 每 --state-flush-every 块 flush 一次（输出 CSV 先 flush、state 后 flush，
    崩溃最多导致输出重复行，不会丢行）；启动时加载入 set 去重，末行写坏按
    坏行忽略告警；
  - failed / parse_error 不记 done：本轮只计数跳过（extract_one 内部已按
    --retries 重试），下轮（监督器重启）自动重试。

内存红线：不缓存块样本、不把结果攒在内存（完成即流式写 CSV）；唯一大对象
是 done set（约 130 万 chunk_id，~200MB，可接受）。在飞 future 窗口有界
（≤ 2×workers），提交端不会无界排队。

退出码：0 全量完成（或 --limit 截断且无失败块）；3 本轮扫完但仍有
failed/parse_error（监督器重启即自动重试这些块）；130 收到停止信号
（SIGTERM/SIGINT，已 flush 断点，重跑续传）；2 输入错误。

用法（仓库根目录）：
  python -u scripts/extract_propositions_full.py                # 全量
  python -u scripts/extract_propositions_full.py --limit 300    # 冒烟
长跑由 kg/import/supervise_extract_full.sh 监督（异常退出 → qwen 健康检查
→ 重启续跑，断点在 state 文件里）。
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import threading
import time
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                as_completed, wait)
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]

# 复用试点组件（prompt/解析/归一/客户端/流式读全部单一来源）。
# 直接运行本脚本时 sys.path[0] 是 scripts/，退回平级导入（compare_thinking_ab.py 同法）。
try:
    from scripts import extract_propositions_pilot as ep  # type: ignore
except ImportError:  # pragma: no cover —— 直接运行路径，测试走上一分支
    import extract_propositions_pilot as ep  # type: ignore

# ---------------------------------------------------------------- 默认配置

DEFAULT_CHUNKS = "data/pilot/documents_chunks.csv"
DEFAULT_OUT = "data/props_full/propositions_full.csv"
DEFAULT_STATE = "kg/import/checkpoints/extract_full_state.jsonl"
DEFAULT_LOG_EVERY = 1000
DEFAULT_STATE_FLUSH_EVERY = 64
#: 在飞 future 数上界 = workers × 2（提交端防内存堆积；实际并发仍由 workers 决定）
WINDOW_FACTOR = 2
#: 输入总行数粗估值（已核验：1,306,907 块），仅用于进度/ETA 显示
TOTAL_ESTIMATE = 1_306_907

EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_HAS_FAILURES = 3
EXIT_INTERRUPTED = 130

#: parse_error/failed 逐块日志上限（超出只进计数，日志量有界）
MAX_ERR_LOG = 20


def _default_log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 断点 state


class DoneState:
    """已完成 chunk_id 集合，JSON Lines 追加持久化（BackfillState 模式）。

    一行 {"chunk_id": ..., "status": "ok"|"empty"}；ok/empty 记 done，
    failed/parse_error 由调用方不写入（下轮自动重试）。末行写坏（进程被杀）
    在加载时按坏行忽略并告警。追加而非整文件重写，大进度下每批 O(1)。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.done: set[str] = set()
        bad = 0
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        chunk_id = str(rec["chunk_id"]).strip()
                        status = rec.get("status")
                    except (ValueError, KeyError, TypeError):
                        bad += 1
                        continue
                    if chunk_id and status in ("ok", "empty"):
                        self.done.add(chunk_id)
            if bad:
                print(f"[警告] state 文件 {self.path} 有 {bad} 行无法解析"
                      f"（忽略，按未完成处理）", file=sys.stderr, flush=True)
        self._fh = None

    def mark(self, chunk_id: str, status: str) -> None:
        """记一块完成：追加一行并加入 done set（不 flush，由调用方批量 flush）。"""
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
            # 末行写坏（进程被杀）常缺换行符：先补一个，避免追加行与坏行粘连
            if self.path.stat().st_size > 0:
                with self.path.open("rb") as rb:
                    rb.seek(-1, 2)
                    if rb.read(1) != b"\n":
                        self._fh.write("\n")
        self._fh.write(json.dumps({"chunk_id": chunk_id, "status": status},
                                  ensure_ascii=False) + "\n")
        self.done.add(chunk_id)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- 输出 CSV


class OutputCsv:
    """追加式命题输出 CSV。

    新建文件时用 utf-8-sig 写 BOM+列头（与试点产物一致）；追加已有文件时用
    纯 utf-8——utf-8-sig 的追加模式会在文件中部再插一个 BOM（试点
    append_rows 多次追加的隐患，这里规避）。整个运行期持有一个小缓冲句柄，
    完成即写，不在内存攒行。
    """

    def __init__(self, path: str | Path, columns: list[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.is_file() or self.path.stat().st_size == 0
        self._fh = self.path.open(
            "a", encoding="utf-8-sig" if fresh else "utf-8", newline="")
        self._writer = csv.writer(self._fh)
        if fresh:
            self._writer.writerow(columns)
            self._fh.flush()
        self.rows = 0

    def writerows(self, rows: list[list[str]]) -> None:
        self._writer.writerows(rows)
        self.rows += len(rows)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


# ---------------------------------------------------------------- 块条目


def build_item(row: dict[str, str], chunk_id: str, content: str,
               max_chars: int) -> dict[str, str]:
    """构造与试点 _sample_item 同构的块条目（供试点 build_messages/extract_one）。

    送入 LLM 的文本 = 截断后的 content，与 source_field 派生输入一致。
    """
    truncated = content[:max_chars]
    return {
        "chunk_id": chunk_id,
        "doc_id": (row.get("doc_id") or "").strip(),
        "title": (row.get("title") or "").strip(),
        "service_id": (row.get("service_id") or "").strip(),
        "category_l1": (row.get("category_l1") or "").strip() or "未知",
        "source_field": ep.derive_source_field(truncated),
        "chunk_no": (row.get("chunk_no") or "").strip(),
        "content": truncated,
    }


# ---------------------------------------------------------------- CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="extract_propositions_full",
        description="全量命题抽取：documents_chunks.csv 全部块流式抽取为"
                    "上下文独立命题（复用试点 pilot-v1 prompt，默认关思考，"
                    "JSONL 断点续跑）",
        epilog="环境变量：EXTRACT_BASE_URL / EXTRACT_MODEL / EXTRACT_API_KEY"
               "（同试点）。长跑建议由 kg/import/supervise_extract_full.sh 监督。",
    )
    parser.add_argument("--chunks", metavar="CSV", default=DEFAULT_CHUNKS,
                        help=f"chunks 输入 CSV（默认 {DEFAULT_CHUNKS}，流式读取）")
    parser.add_argument("--out", metavar="CSV", default=DEFAULT_OUT,
                        help=f"命题输出 CSV（默认 {DEFAULT_OUT}，追加写）")
    parser.add_argument("--state-file", metavar="JSONL", default=DEFAULT_STATE,
                        help=f"断点 state JSONL（默认 {DEFAULT_STATE}）")
    parser.add_argument("--workers", type=int, default=ep.DEFAULT_WORKERS,
                        metavar="N",
                        help=f"并发数（默认 {ep.DEFAULT_WORKERS}，用户既定配置）")
    parser.add_argument("--timeout", type=float, default=ep.DEFAULT_TIMEOUT,
                        metavar="SEC",
                        help=f"单请求超时秒（默认 {ep.DEFAULT_TIMEOUT}）")
    parser.add_argument("--max-tokens", type=int, default=ep.LLM_MAX_TOKENS,
                        metavar="N",
                        help=f"请求 max_tokens（默认 {ep.LLM_MAX_TOKENS}）")
    parser.add_argument("--retries", type=int, default=1, metavar="N",
                        help="单块重试次数（默认 1，即最多调用 1+1 次；超过即"
                             "跳过该块计数，下轮自动重试）")
    parser.add_argument("--min-chunk-chars", type=int, default=30, metavar="N",
                        help="短块过滤阈值（默认 30 字符）")
    parser.add_argument("--max-chunk-chars", type=int, default=1500, metavar="N",
                        help="送入 LLM 的文本截断上限（默认 1500 字符）")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="最多提交块数（默认 0=全量；冒烟用）")
    parser.add_argument("--log-every", type=int, default=DEFAULT_LOG_EVERY,
                        metavar="N", help=f"进度日志间隔（默认每 {DEFAULT_LOG_EVERY} 块）")
    parser.add_argument("--state-flush-every", type=int,
                        default=DEFAULT_STATE_FLUSH_EVERY, metavar="N",
                        help=f"state/输出 flush 间隔（默认每 {DEFAULT_STATE_FLUSH_EVERY} 块）")
    parser.add_argument("--total", type=int, default=TOTAL_ESTIMATE,
                        metavar="N",
                        help=f"输入总块数粗估值（默认 {TOTAL_ESTIMATE}，仅用于"
                             f"进度/ETA 显示）")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers 必须 >= 1")
    if args.timeout <= 0:
        parser.error("--timeout 必须 > 0")
    if args.max_tokens < 1:
        parser.error("--max-tokens 必须 >= 1")
    if args.retries < 0:
        parser.error("--retries 必须 >= 0")
    if args.min_chunk_chars < 0:
        parser.error("--min-chunk-chars 必须 >= 0")
    if args.max_chunk_chars < args.min_chunk_chars:
        parser.error("--max-chunk-chars 必须 >= --min-chunk-chars")
    if args.limit < 0:
        parser.error("--limit 必须 >= 0")
    if args.log_every < 1:
        parser.error("--log-every 必须 >= 1")
    if args.state_flush_every < 1:
        parser.error("--state-flush-every 必须 >= 1")
    if args.total < 0:
        parser.error("--total 必须 >= 0")
    return args


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None,
         client_factory: Callable[..., ep.LlmClient] | None = None,
         log: Callable[[str], None] = _default_log,
         executor_factory: Callable[[int], ThreadPoolExecutor] | None = None,
         stop_event: threading.Event | None = None) -> int:
    """全量抽取一轮。返回进程退出码（见模块 docstring）。

    client_factory / executor_factory / stop_event 供单测注入；stop_event
    为 None 时注册 SIGTERM/SIGINT 处理器（置位事件，主循环停止提交、等在飞
    请求收尾后 flush 退出）。
    """
    args = parse_args(argv)

    if stop_event is None:
        stop = threading.Event()

        def _request_stop(signum, frame):  # noqa: ARG001
            stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _request_stop)
    else:
        stop = stop_event

    state = DoneState(args.state_file)
    done_at_start = len(state.done)

    client = (client_factory or ep.LlmClient)(
        timeout=args.timeout, max_tokens=args.max_tokens)  # enable_thinking=False 默认
    out_csv = OutputCsv(args.out, list(ep.OUT_COLUMNS))

    log(f"[启动] chunks={args.chunks} out={args.out} state={args.state_file}")
    log(f"[启动] workers={args.workers} timeout={args.timeout}s "
        f"max_tokens={args.max_tokens} retries={args.retries} "
        f"model={client.model} prompt={ep.PROMPT_VERSION} 思考链=关")
    log(f"[断点] 已加载 {done_at_start} 块 done；在飞窗口 ≤ "
        f"{args.workers * WINDOW_FACTOR}，flush 间隔 {args.state_flush_every} 块")

    stats = {"scanned": 0, "skipped_short": 0, "skipped_no_id": 0,
             "skipped_done": 0, "submitted": 0, "ok": 0, "empty": 0,
             "parse_error": 0, "failed": 0, "prop_rows": 0}
    err_logged = 0
    interrupted = False
    limit_hit = False
    t_start = time.time()
    t_extract: float | None = None
    window = args.workers * WINDOW_FACTOR
    pending: dict = {}

    def handle(fut, item: dict[str, str]) -> None:
        """处理一个完成的 future：写输出行、记账、按节奏 flush 与打进度。"""
        nonlocal err_logged
        try:
            result = fut.result()
        except Exception as e:  # noqa: BLE001 —— 防御：extract_one 不应抛
            result = {"status": "failed",
                      "error": f"{type(e).__name__}: {e}",
                      "propositions": [], "norm": {}}
        status = result.get("status", "failed")
        if status == "ok":
            # 先写输出行、后记 done：崩溃最多造成输出重复行，不会丢行
            rows = [[item["chunk_id"], item["service_id"], item["doc_id"],
                     item["source_field"], prop["predicate"],
                     prop["condition_type"], prop["object_value"],
                     prop["statement"]]
                    for prop in result.get("propositions", [])]
            out_csv.writerows(rows)
            stats["prop_rows"] += len(rows)
            stats["ok"] += 1
            state.mark(item["chunk_id"], "ok")
        elif status == "empty":
            stats["empty"] += 1
            state.mark(item["chunk_id"], "empty")
        elif status == "parse_error":
            stats["parse_error"] += 1
            if err_logged < MAX_ERR_LOG:
                err_logged += 1
                log(f"[parse_error] {item['chunk_id']}: "
                    f"{result.get('error', '')}；原始输出前 200 字："
                    f"{result.get('raw_head', '')!r}")
        else:
            stats["failed"] += 1
            if err_logged < MAX_ERR_LOG:
                err_logged += 1
                log(f"[failed] {item['chunk_id']}: {result.get('error', '')}")

        completed = (stats["ok"] + stats["empty"]
                     + stats["parse_error"] + stats["failed"])
        if completed % args.state_flush_every == 0:
            out_csv.flush()
            state.flush()
        if completed % args.log_every == 0:
            elapsed = max(time.time() - (t_extract or t_start), 1e-6)
            rate = completed / elapsed
            done_now = done_at_start + completed
            eta = ""
            if args.total > 0 and rate > 0:
                remain = max(args.total - done_now, 0)
                eta = f"，粗估剩余 {remain / rate / 3600:.1f} 小时"
            log(f"[进度] 本次完成 {completed} 块"
                f"（累计 done {done_now}/{args.total}），"
                f"扫描 {stats['scanned']} 行，速率 {rate:.2f} 块/s，"
                f"ok={stats['ok']} empty={stats['empty']} "
                f"parse_error={stats['parse_error']} "
                f"failed={stats['failed']}{eta}")

    factory = executor_factory or (lambda w: ThreadPoolExecutor(max_workers=w))
    try:
        with factory(args.workers) as pool:
            for row in ep.iter_chunk_rows(args.chunks):
                if stop.is_set():
                    interrupted = True
                    break
                stats["scanned"] += 1
                content = (row.get("content") or "").strip()
                if len(content) < args.min_chunk_chars:
                    stats["skipped_short"] += 1
                    continue
                chunk_id = (row.get("chunk_id") or "").strip()
                if not chunk_id:
                    stats["skipped_no_id"] += 1
                    continue
                if chunk_id in state.done:
                    stats["skipped_done"] += 1
                    continue
                item = build_item(row, chunk_id, content, args.max_chunk_chars)
                # 有界窗口：在飞 future 数 ≤ 2×workers，提交端不无界排队
                while len(pending) >= window:
                    done_futs, _ = wait(set(pending),
                                        return_when=FIRST_COMPLETED)
                    for fut in done_futs:
                        handle(fut, pending.pop(fut))
                if t_extract is None:
                    t_extract = time.time()
                pending[pool.submit(ep.extract_one, client, item,
                                    retries=args.retries)] = item
                stats["submitted"] += 1
                if args.limit and stats["submitted"] >= args.limit:
                    limit_hit = True
                    break
            # 收尾：等在飞请求全部完成（窗口有界，等待有界）
            for fut in as_completed(list(pending)):
                handle(fut, pending.pop(fut))
    except KeyboardInterrupt:
        # SIGINT 未被接管时的兜底（如非主线程注册失败）：直接走 finally flush
        interrupted = True
    finally:
        try:
            out_csv.flush()
            state.flush()
        finally:
            out_csv.close()
            state.close()

    elapsed = time.time() - t_start
    extracted = stats["ok"] + stats["empty"]
    rate_note = ""
    attempted = extracted + stats["parse_error"] + stats["failed"]
    if t_extract is not None and attempted > 0:
        dur = max(time.time() - t_extract, 1e-6)
        rate_note = f"，抽取速率 {attempted / dur:.2f} 块/s"
    log(f"[汇总] 耗时 {elapsed:.1f}s：扫描 {stats['scanned']} 行"
        f"（短块跳过 {stats['skipped_short']}，无 chunk_id 跳过 "
        f"{stats['skipped_no_id']}，断点跳过 {stats['skipped_done']}），"
        f"本次提交 {stats['submitted']} 块：成功 {stats['ok']}，空抽 "
        f"{stats['empty']}，parse_error {stats['parse_error']}，failed "
        f"{stats['failed']}；新增命题 {stats['prop_rows']} 行 → {args.out}"
        f"{rate_note}；state 累计 {len(state.done)} 块")
    if limit_hit:
        log(f"[结束] 达到 --limit {args.limit}，本轮提前截断（exit 由失败块决定）")

    if interrupted or stop.is_set():
        log(f"[中断] 收到停止信号，断点已 flush（exit {EXIT_INTERRUPTED}），"
            f"重跑将从断点续传")
        return EXIT_INTERRUPTED
    if stats["parse_error"] or stats["failed"]:
        log(f"[结束] 本轮完成但有 {stats['parse_error'] + stats['failed']} 个"
            f"失败块未记 done（exit {EXIT_HAS_FAILURES}），下轮自动重试")
        return EXIT_HAS_FAILURES
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
