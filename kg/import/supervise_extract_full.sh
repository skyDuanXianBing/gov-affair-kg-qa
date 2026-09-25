#!/bin/bash
# 全量命题抽取监督器（单实例保护版）：extract_propositions_full.py 异常退出（非 0）
# 时先做 qwen 健康检查（不通则每 60s 再查，最多等 30 分钟），再重启续跑——断点在
# kg/import/checkpoints/extract_full_state.jsonl，重启自动跳过已完成块。
#
# 子进程退出码约定（见 scripts/extract_propositions_full.py 模块 docstring）：
#   0   全量完成（正常结束监督）
#   3   本轮扫完但仍有 failed/parse_error 块（重启重试这些块）
#   130 收到停止信号（重启续传；要彻底停请先 kill 本监督器）
#   2   输入错误等（健康检查后重启，轮次上限兜底）
#
# 用法（仓库根目录，Git Bash）：
#   bash kg/import/supervise_extract_full.sh
# 日志：kg/import/checkpoints/extract_full_run.log（子进程 stdout/stderr +
# 监督器轮次记录）。
#
# 单实例保护（2026-09-18 双栈事故后新增）：Git Bash 无 flock，锁由与监督器
# 同寿的 bash 进程持有——方式是把锁检测放在监督器自身，用一个独占打开且
# 不关闭的文件句柄（FD 9）作为信号量；配合 python 的 msvcrt 锁做跨平台互斥。
# 任何入口（开机 VBS / 计划任务 / 手动）拿到锁前已检测到实例就 exit 0。

cd "E:/Graudate/gov-affair-kg-qa/repo" || exit 1

LOCK="kg/import/checkpoints/extract_full.lock"
LOG="kg/import/checkpoints/extract_full_run.log"
QWEN_HEALTH_URL="http://10.130.71.10:30799/v1/models"
MAX_ROUNDS=200

mkdir -p kg/import/checkpoints data/propositions

# ---- 单实例互斥 ----
# 常驻 python 子进程持有锁，父 bash 用其退出码判定；该子进程与监督器同寿
# （bash 退出时它被杀）。锁文件打开后保持，进程死则锁自动释放。
python - "$LOCK" <<'PYEOF' &
import sys, os, time
lock_path = sys.argv[1]
if os.name == 'nt':
    import msvcrt
    f = open(lock_path, 'a+b')
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        print('LOCK_BUSY', flush=True)
        sys.exit(2)          # 已有实例持有锁
    f.seek(0); f.truncate()
    f.write(('pid=%s started=%s\n' % (os.getpid(), time.strftime('%F %T'))).encode())
    f.flush()
    print('LOCK_OK pid=%d' % os.getpid(), flush=True)
    # 常驻：不退出即不释放锁；父死则管道断开/被杀，fd 关闭锁释放
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        pass
else:
    import fcntl
    f = open(lock_path, 'a+b')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        print('LOCK_BUSY', flush=True)
        sys.exit(2)
    print('LOCK_OK pid=%d' % os.getpid(), flush=True)
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        pass
PYEOF
LOCK_PID=$!

# 等待锁判定结果（LOCK_BUSY / LOCK_OK），最多 3 秒
sleep 3
if ! kill -0 "$LOCK_PID" 2>/dev/null; then
  # python 已退出（exit 2 = 锁被占），无锁可拿 → 放弃本次启动
  echo "[supervisor] another instance holds the lock, exiting $(date '+%F %T')" >> "$LOG"
  exit 0
fi

echo "[supervisor] started pid=$$ lock_pid=$LOCK_PID $(date '+%F %T')" >> "$LOG"
for i in $(seq 1 "$MAX_ROUNDS"); do
  python -u scripts/extract_propositions_full.py \
    --out data/propositions/propositions_full.csv >> "$LOG" 2>&1
  code=$?
  echo "[supervisor] round $i exit=$code $(date '+%F %T')" >> "$LOG"
  if [ "$code" -eq 0 ]; then
    echo "[supervisor] EXTRACT FULL COMPLETE" >> "$LOG"
    kill "$LOCK_PID" 2>/dev/null
    exit 0
  fi
  echo "[supervisor] child exited $code, checking qwen health before restart" >> "$LOG"
  healthy=0
  # 30 次 × 60s = 最多等约 30 分钟；curl -m 5 超时不计入等待上限
  for w in $(seq 1 30); do
    if curl -s -m 5 "$QWEN_HEALTH_URL" > /dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 60
  done
  if [ "$healthy" -eq 1 ]; then
    echo "[supervisor] qwen healthy, restarting extractor (round $((i + 1)))" >> "$LOG"
  else
    echo "[supervisor] qwen unreachable for ~30min, restarting anyway (round $((i + 1)))" >> "$LOG"
  fi
done
echo "[supervisor] gave up after $MAX_ROUNDS rounds" >> "$LOG"
kill "$LOCK_PID" 2>/dev/null
exit 1
