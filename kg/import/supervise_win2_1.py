#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 导入监督器 v2.1（串行降并发版，2026-09-01 晨）。

v2 的教训：3-4 个 Builder Job 并发写会触发 Docker Desktop 跨界 IO 卡死
（08-31 20:54、09-01 08:49 两次，事务集体 Closing 20 分钟零推进）；
Neo4j 重启恢复需回放 34GB txlog（30-60 分钟），"卡死→重启"循环代价过高。
单道实测（faqs 126K 行 1.6h ≈ 3 并发时单片 8-10h 的 5 倍速），故全面转串行：

  0. ensure mock_llm(18999)
  1. repair（无等待）：修已终态分片
  2. repair --wait：等存量 RUNNING job（23/24/25 等）全部到终态并修正
     ——消化期不提交任何新 job；等待上限 48h，超限退出码 2（人工介入信号）
  3. 弱实体续跑（1 道串行：process_steps → service_channels → fees/其它）
  4. repair --wait + services 续跑（1 道）
  5. sweep 兜底
  6. 关系 13 表（2 道：小表道 + 大表道，写入并发 ≤2）
  7. sweep 兜底
  8. service_routing（1 道）
  9. sweep 兜底
 10. verify_graph.py

日志：kg/import/checkpoints/win_supervisor.log + 每道 win_*.log。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CKPT = ROOT / "kg" / "import" / "checkpoints"
STATE = CKPT / "state_win.json"
TIMEOUT = "172800"  # 48h：单片实测最长 ~20h（services 片1），留足余量
LOG = CKPT / "win_supervisor.log"

RUN = [sys.executable, "-X", "utf8", str(ROOT / "kg" / "import" / "run_import.py")]
REPAIR = [sys.executable, "-X", "utf8", str(ROOT / "kg" / "import" / "repair_state_win.py")]
COMMON = ["--execute", "--wait", "--continue-on-error", "--timeout", TIMEOUT,
          "--state-file", str(STATE)]


def log(msg: str) -> None:
    line = f"{datetime.now():%F %T} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_mock() -> None:
    if port_open(18999):
        log("[A] mock_llm 18999 已在监听")
        return
    mock_log = (CKPT / "win_mock_llm.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "kg" / "import" / "mock_llm_threaded.py"), "18999"],
        stdout=mock_log, stderr=subprocess.STDOUT, cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(20):
        if port_open(18999):
            log(f"[A] mock_llm 已启动 pid={proc.pid}")
            return
        time.sleep(0.5)
    log("[A] 警告：mock_llm 未在 10s 内监听 18999（导入仍可能正常，Mac 经验：URL 失效不阻塞）")


def run_cmd(name: str, cmd: list[str]) -> int:
    out = (CKPT / f"win_{name}.log").open("ab")
    return subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT)).returncode


def run_lane(name: str, tables: list[str]) -> subprocess.Popen:
    out = (CKPT / f"win_{name}.log").open("ab")
    cmd = RUN + ["--tables", ",".join(tables)] + COMMON
    return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))


def stage(n: str, lanes: list[tuple[str, list[str]]]) -> None:
    log(f"[{n}] 启动 {len(lanes)} 道: {[t for _, t in lanes]}")
    procs = [(nm, run_lane(nm, tb)) for nm, tb in lanes]
    for nm, p in procs:
        rc = p.wait()
        log(f"[{n}] {nm} 退出码 {rc}")


def repair(wait: bool, wait_timeout: int = 172800) -> None:
    tag = "repair-wait" if wait else "repair"
    cmd = REPAIR + (["--wait", "--wait-timeout", str(wait_timeout)] if wait else [])
    log(f"[{tag}] 核对/修正 checkpoint（wait={wait}）")
    rc = run_cmd(tag, cmd)
    log(f"[{tag}] 退出码 {rc}")


def pending_tables() -> list[str]:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    keys: list[str] = []
    for key, entry in state["parts"].items():
        if entry.get("status") != "SUCCESS":
            job_key = str(entry.get("job_key") or key.split(":")[0])
            if job_key not in keys:
                keys.append(job_key)
    return keys


def drain_running(deadline_s: int = 172800) -> bool:
    """等待存量 RUNNING job 全部到终态（不新增提交），返回是否排空。"""
    started = time.time()
    while time.time() - started < deadline_s:
        repair(wait=True, wait_timeout=max(1800, int(deadline_s - (time.time() - started))))
        tables = pending_tables()
        if not tables:
            return True
        log(f"[drain] 仍有非 SUCCESS 分片（{tables}），5 分钟后重查（服务端 job 消化期）")
        time.sleep(300)
    log("[drain] 超过 48h 仍未排空，退出码 2（人工介入）")
    return False


def sweep(n: str) -> None:
    repair(wait=True)
    tables = pending_tables()
    if not tables:
        log(f"[{n}] 全部分片 SUCCESS，无需兜底")
        return
    log(f"[{n}] 兜底重跑 {len(tables)} 表: {tables}")
    rc = run_cmd(f"sweep-{n}", RUN + ["--tables", ",".join(tables)] + COMMON)
    log(f"[{n}] 兜底退出码 {rc}")


def main() -> int:
    os.chdir(ROOT)
    CKPT.mkdir(parents=True, exist_ok=True)
    log("=== v2.1 串行降并发监督器启动（先排空存量 job，再单道串行） ===")
    ensure_mock()
    repair(wait=False)

    # 消化期：等 Job 23/24/25 等存量到终态，期间不提交任何新 job
    if not drain_running():
        return 2

    stage("3", [("weak", ["process_steps", "service_channels", "fees",
                          "results", "faqs", "conditions"])])
    repair(wait=True)
    stage("4", [("services", ["services"])])
    sweep("4.5")

    # 关系表 2 道：小表道 + 大表道（写入并发 ≤2，大表 service_based_on 单道串行逐片）
    stage("5", [("rel-small", ["service_handled_by", "service_collaborates_with",
                               "service_has_condition", "service_has_faq",
                               "service_has_fee", "process_step_next",
                               "service_produces_result", "service_has_channel",
                               "part_of"]),
                ("rel-big", ["service_requires_material", "service_based_on",
                             "service_has_process_step"])])
    sweep("5.5")

    stage("6", [("sr", ["service_belongs_to_domain", "service_uses_model",
                        "service_classified_as"])])
    sweep("6.5")

    log("[7] 对账 verify_graph.py ...")
    rc = run_cmd("verify", [sys.executable, "-X", "utf8",
                            str(ROOT / "kg" / "import" / "verify_graph.py")])
    log(f"[7] verify_graph 退出码 {rc}")
    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
