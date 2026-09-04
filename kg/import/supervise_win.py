#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 全量导入监督器（迁移自 supervise_v5.sh 逻辑，纯 Python 免 bash 依赖）。

阶段（依赖序，参照 manifest IMPORT_ORDER：routing→routing_rel→shared→weak→services→relations）：
  A. 确保 mock_llm_threaded(18999) 存活（Builder vectorize 占位服务）
  1. 路由层 9 表（小，单道顺序）
  2. 共享实体 4 表（小，单道顺序）
  3. 弱实体 6 表（3 道并行）
  4. services（长行表，单独一道，降并发防堆抖动）
  5. 关系表 13 表（5 道并行）
  6. service_routing 3 表（2 道并行）
  7. verify_graph.py 对账

checkpoint 用全新 state_win.json（Windows 全新栈不认 Mac 的 state.json，仅参考）。
日志：kg/import/checkpoints/win_*.log。
"""

from __future__ import annotations

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
TIMEOUT = "28800"
LOG = CKPT / "win_supervisor.log"

RUN = [sys.executable, "-X", "utf8", str(ROOT / "kg" / "import" / "run_import.py")]
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


def main() -> int:
    os.chdir(ROOT)
    CKPT.mkdir(parents=True, exist_ok=True)
    log("=== Windows 全量导入监督器启动 ===")
    ensure_mock()

    stage("1", [("routing", ["service_domains", "category_schemes", "service_categories",
                             "knowledge_models", "category_parent", "category_belongs_to_scheme",
                             "category_belongs_to_domain", "model_applies_to_category",
                             "model_belongs_to_domain", "model_extends_model"])])
    stage("2", [("shared", ["departments", "materials", "legal_bases", "legal_citations"])])
    stage("3", [("weak-a", ["conditions"]),
                ("weak-b", ["process_steps", "results"]),
                ("weak-c", ["faqs", "service_channels", "fees"])])
    stage("4", [("services", ["services"])])
    stage("5", [("rel-a", ["service_handled_by", "service_collaborates_with",
                           "service_has_condition", "service_has_faq"]),
                ("rel-b", ["service_requires_material", "service_has_fee"]),
                ("rel-c", ["service_based_on"]),
                ("rel-d", ["service_has_process_step", "process_step_next",
                           "service_produces_result"]),
                ("rel-e", ["service_has_channel", "part_of"])])
    stage("6", [("sr-a", ["service_belongs_to_domain", "service_uses_model"]),
                ("sr-b", ["service_classified_as"])])

    log("[7] 对账 verify_graph.py ...")
    with (CKPT / "win_verify.log").open("wb") as out:
        rc = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "kg" / "import" / "verify_graph.py")],
                            stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT)).returncode
    log(f"[7] verify_graph 退出码 {rc}")
    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
