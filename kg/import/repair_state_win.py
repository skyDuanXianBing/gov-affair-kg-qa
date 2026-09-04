#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修正 checkpoint：把"等待方超时退出、Builder 侧实际已完成"的分片标为 SUCCESS。

背景：run_import.py 的 wait_for_job 受 --timeout 上限（监督器曾用 28800s），Windows
实测单片最长 10.5h；超时后 run_import 把分片留在 SUBMITTED 即退出，但 Builder Job
在服务端继续跑并最终 FINISH。重跑时 run_import 只跳过 SUCCESS 分片，SUBMITTED 会
重复上传导入，因此先跑本脚本核对 Builder Job 真实状态再续跑。

规则：
  - job 终态为 FINISH/SUCCESS  → 分片改 status=SUCCESS（带 builder_status、耗时）
  - job 终态为 FAILURE/ERROR 等 → 删除分片条目，让续跑重新提交
  - job 仍在 RUNNING → 默认跳过；--wait 时轮询到终态再按上述规则处理

写入走与 run_import 相同的 .lock 文件锁（msvcrt/fcntl），锁内重读文件后应用修改，
不回退其它进程写入的更新。用法：
  python -X utf8 kg/import/repair_state_win.py [--wait] [--state-file PATH]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from spg_client import TERMINAL_FAILURE, TERMINAL_SUCCESS, SpgClient, SpgClientError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / "kg" / "import" / "checkpoints" / "state_win.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lock_pair():
    if importlib_os_nt():
        import msvcrt

        def _lock(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

        def _unlock(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def _lock(fh):
            fcntl.flock(fh, fcntl.LOCK_EX)

        def _unlock(fh):
            fcntl.flock(fh, fcntl.LOCK_UN)
    return _lock, _unlock


def importlib_os_nt() -> bool:
    import os

    return os.name == "nt"


def parse_job_time(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def fetch_status(client: SpgClient, job_id: int) -> tuple[str, dict]:
    job = client.get_job(job_id)
    return str(job.get("status") or "UNKNOWN").upper(), job


def apply_fixes(
    state_path: Path, fixes: dict[str, str], job_meta: dict[int, dict]
) -> None:
    """锁内读-改-写：fixes[state_key]=action，action in {SUCCESS, DROP}。"""
    _lock, _unlock = _lock_pair()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_fh:
        _lock(lock_fh)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            parts = state["parts"]
            for key, action in fixes.items():
                entry = parts.get(key)
                if entry is None:
                    continue
                if action == "SUCCESS":
                    job_id = int(entry["job_id"])
                    meta = job_meta.get(job_id, {})
                    created = parse_job_time(str(meta.get("gmtCreate") or ""))
                    modified = parse_job_time(str(meta.get("gmtModified") or ""))
                    elapsed = (
                        int((modified - created).total_seconds())
                        if created and modified
                        else None
                    )
                    entry.update(
                        {
                            "status": "SUCCESS",
                            "builder_status": "FINISH",
                            "repaired_at": now_iso(),
                            "updated_at": now_iso(),
                        }
                    )
                    if elapsed is not None:
                        entry["elapsed_seconds"] = elapsed
                elif action == "DROP":
                    parts.pop(key, None)
            state["updated_at"] = now_iso()
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        finally:
            _unlock(lock_fh)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--wait", action="store_true", help="等待 RUNNING 的 job 到终态")
    parser.add_argument("--wait-timeout", type=int, default=93600,
                        help="--wait 的最长等待秒数（默认 26h，防服务重启后 job 卡 RUNNING）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8887")
    parser.add_argument("--poll-interval", type=int, default=60)
    args = parser.parse_args()

    client = SpgClient(args.base_url)
    pending: list[tuple[str, int]] = []  # (state_key, job_id)

    state = json.loads(args.state_file.read_text(encoding="utf-8"))
    for key, entry in state["parts"].items():
        if entry.get("status") == "SUCCESS" or not entry.get("job_id"):
            continue
        pending.append((key, int(entry["job_id"])))

    if not pending:
        print(f"[repair] 无待核对分片（非 SUCCESS 且已提交 job 的条目为 0）")
        return 0

    names = ["%s#%s" % (key.split(":")[0], job_id) for key, job_id in pending]
    print(f"[repair] 待核对分片 {len(pending)} 个: {names}")
    fixes: dict[str, str] = {}
    job_meta: dict[int, dict] = {}
    waiting = list(pending)
    wait_started = time.time()
    while waiting:
        still: list[tuple[str, int]] = []
        for key, job_id in waiting:
            try:
                status, job = fetch_status(client, job_id)
            except (SpgClientError, OSError) as exc:
                print(f"[repair] 查询 Job {job_id} 失败（稍后重试）: {exc}")
                still.append((key, job_id))
                continue
            job_meta[job_id] = job
            if status in TERMINAL_SUCCESS:
                fixes[key] = "SUCCESS"
                print(f"[repair] {key} -> Job {job_id} FINISH，标 SUCCESS")
            elif status in TERMINAL_FAILURE:
                fixes[key] = "DROP"
                print(f"[repair] {key} -> Job {job_id} {status}，删除条目待重跑")
            else:
                if args.wait:
                    still.append((key, job_id))
                else:
                    print(f"[repair] {key} -> Job {job_id} 仍 {status}，本次跳过")
        if not args.wait:
            break
        waiting = still
        if waiting:
            if time.time() - wait_started > args.wait_timeout:
                print(f"[repair] 等待超过 {args.wait_timeout}s 仍有 RUNNING job "
                      f"（{[j for _, j in waiting]}），放弃等待（可能是重启后卡死的 job，"
                      f"由后续 sweep 兜底重跑）")
                break
            print(
                f"[repair] 等待 {len(waiting)} 个 RUNNING job "
                f"（{[j for _, j in waiting]}），{args.poll_interval}s 后重查 …"
            )
            time.sleep(args.poll_interval)

    if fixes:
        apply_fixes(args.state_file, fixes, job_meta)
        print(f"[repair] 已写回 {len(fixes)} 条修正")
    else:
        print("[repair] 无可修正条目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
