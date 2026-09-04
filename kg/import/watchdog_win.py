#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neo4j 写入停滞看门狗（2026-08-31 事件驱动的保守自愈）。

背景：今晚 20:54 起 4 个 Builder Job 并发写时，Neo4j（Docker Desktop gRPC-FUSE
卷）数十个写事务卡在 Closing 状态 20 分钟零推进，server 处理线程全部阻塞。
docker restart neo4j 后完全恢复（事务原子回滚、server driver 自动重连、
GovernmentService 导入进度无缝继续，均已实证）。

策略（保守）：
  - 每 5 分钟查一次 SHOW TRANSACTIONS；
  - 仅当存在 zwdmxgj 事务处于 Closing 状态且已持续 >15 分钟（正常 Closing <2s）
    且 checkpoint 中确有非 SUCCESS 分片（导入确在进行）时，restart neo4j；
  - 重启后冷却 5 分钟再进入下一轮；单日最多重启 6 次防抖动；
  - 全部动作记录 win_watchdog.log。

部署：schtasks GovKGWatchdog（SYSTEM，onstart 自启 + 手动启动一次）。
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CKPT = ROOT / "kg" / "import" / "checkpoints"
STATE = CKPT / "state_win.json"
LOG = CKPT / "win_watchdog.log"
DOCKER = [r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
          "--config", r"E:\graduate\gov-affair-kg-qa\docker_cfg"]
NEO4J = "release-openspg-neo4j"
CLOSING_STALE_SEC = 900   # Closing 超过 15 分钟视为卡死
CHECK_INTERVAL = 300      # 5 分钟
COOLDOWN_AFTER_RESTART = 300
MAX_RESTARTS_PER_DAY = 6


def log(msg: str) -> None:
    line = f"{datetime.now():%F %T} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def parse_duration(text: str) -> float:
    """PT19M48.888S / PT1.016S / PT2H3M4S -> 秒。"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", text or "")
    if not m:
        return 0.0
    h, mi, s = (float(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def import_active() -> bool:
    try:
        import json
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(p.get("status") != "SUCCESS" for p in state["parts"].values())


def stale_closing_secs() -> float:
    """返回最久的 zwdmxgj Closing 事务持续时间（秒）；无则 0。"""
    try:
        proc = subprocess.run(
            DOCKER + ["exec", NEO4J, "cypher-shell", "-u", "neo4j",
                      "-p", "neo4j@openspg", "-d", "system", "SHOW TRANSACTIONS;"],
            capture_output=True, text=True, timeout=90)
        out = proc.stdout
        if proc.returncode != 0 and not out:
            log(f"SHOW TRANSACTIONS 非零退出 rc={proc.returncode} stderr={proc.stderr[:200]}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"SHOW TRANSACTIONS 执行失败（跳过本轮）: {type(exc).__name__}: {exc}")
        return -1.0
    worst = 0.0
    for line in out.splitlines():
        if '"zwdmxgj' not in line or '"Closing"' not in line:
            continue
        m = re.search(r'"(PT[^"]*)"', line)
        if m:
            worst = max(worst, parse_duration(m.group(1)))
    return worst


def restart_neo4j(reason: str) -> None:
    log(f"触发重启 neo4j：{reason}")
    subprocess.run(DOCKER + ["restart", NEO4J], capture_output=True, timeout=300)


def main() -> int:
    log("=== watchdog 启动（Closing>%ds 重启 neo4j，单日上限 %d 次） ==="
        % (CLOSING_STALE_SEC, MAX_RESTARTS_PER_DAY))
    restart_days: dict[str, int] = {}
    while True:
        time.sleep(CHECK_INTERVAL)
        today = datetime.now().strftime("%F")
        if restart_days.get(today, 0) >= MAX_RESTARTS_PER_DAY:
            continue
        if not import_active():
            log("round: 导入全部完成，看门狗转入静默观察")
            time.sleep(3600)
            continue  # 无导入在途，不动作
        worst = stale_closing_secs()
        log(f"round: 最久 Closing {worst:.0f}s")
        if worst < 0:
            continue  # 查询失败，下轮再试
        if worst > CLOSING_STALE_SEC:
            restart_neo4j(f"最久 Closing 事务已 {worst:.0f}s")
            restart_days[today] = restart_days.get(today, 0) + 1
            time.sleep(COOLDOWN_AFTER_RESTART)
        elif worst > 60:
            log(f"观察：最久 Closing {worst:.0f}s（未达阈值，暂不动作）")


if __name__ == "__main__":
    raise SystemExit(main())
