#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 全量导入监督器 v2（断点续跑版，替代 supervise_win.py）。

v1 教训（2026-08-31）：run_import --timeout 28800s 小于 Windows 实测单片时长
（最长 10.5h），等待方超时退出后 v1 不回头重试，弱实体剩余分片被漏掉；
而 Builder Job 实际在服务端跑完。v2 改为：

  0. ensure mock_llm(18999)
  1. repair_state_win.py（无等待）：把已 FINISH 的超时分片标 SUCCESS
  2. 阶段1/2 路由+共享表重过一遍（全 SUCCESS，秒级自检）
  3. 阶段3 弱实体 3 道续跑（SUCCESS 分片跳过，只提交剩余片）
  4. repair --wait：等仍在 RUNNING 的 job（如 services 片1）到终态并修正
  5. 阶段4 services 续跑
  6. repair --wait + 兜底重跑仍有非 SUCCESS 分片的表（一轮）
  7. 阶段5 关系 13 表 5 道
  8. repair --wait + 兜底重跑
  9. 阶段6 service_routing 3 表 2 道
 10. repair --wait + 兜底重跑
 11. verify_graph.py 对账

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
TIMEOUT = "86400"  # 24h：单片实测最长 10.5h，留足余量
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


def repair(wait: bool) -> None:
    tag = "repair-wait" if wait else "repair"
    cmd = REPAIR + (["--wait"] if wait else [])
    log(f"[{tag}] 核对/修正 checkpoint（wait={wait}）")
    rc = run_cmd(tag, cmd)
    log(f"[{tag}] 退出码 {rc}")


def pending_tables() -> list[str]:
    """state 中仍含非 SUCCESS 分片的表（按 manifest 依赖序无关，仅用于兜底重跑）。"""
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


def sweep(n: str) -> None:
    """兜底：repair --wait 后仍有非 SUCCESS 分片的表重跑一轮（SUCCESS 分片自动跳过）。"""
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
    log("=== v2 断点续跑监督器启动（repair + 长超时 + 兜底轮） ===")
    ensure_mock()
    repair(wait=False)

    stage("1", [("routing", ["service_domains", "category_schemes", "service_categories",
                             "knowledge_models", "category_parent", "category_belongs_to_scheme",
                             "category_belongs_to_domain", "model_applies_to_category",
                             "model_belongs_to_domain", "model_extends_model"])])
    stage("2", [("shared", ["departments", "materials", "legal_bases", "legal_citations"])])

    # 弱实体按剩余片数均衡分道：process_steps 4 片 / service_channels 4 片 / fees 1 片
    stage("3", [("weak1", ["process_steps"]),
                ("weak2", ["service_channels"]),
                ("weak3", ["fees", "results", "faqs", "conditions"])])

    # services 片1 的 Job 可能仍在服务端跑：等终态修正后续跑剩余片
    repair(wait=True)
    stage("4", [("services", ["services"])])
    sweep("4.5")

    stage("5", [("rel-a", ["service_handled_by", "service_collaborates_with",
                           "service_has_condition", "service_has_faq"]),
                ("rel-b", ["service_requires_material", "service_has_fee"]),
                ("rel-c", ["service_based_on"]),
                ("rel-d", ["service_has_process_step", "process_step_next",
                           "service_produces_result"]),
                ("rel-e", ["service_has_channel", "part_of"])])
    sweep("5.5")

    stage("6", [("sr-a", ["service_belongs_to_domain", "service_uses_model"]),
                ("sr-b", ["service_classified_as"])])
    sweep("6.5")

    log("[7] 对账 verify_graph.py ...")
    rc = run_cmd("verify", [sys.executable, "-X", "utf8",
                            str(ROOT / "kg" / "import" / "verify_graph.py")])
    log(f"[7] verify_graph 退出码 {rc}")
    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
