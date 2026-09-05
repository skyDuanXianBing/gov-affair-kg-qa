#!/bin/bash
# 回填监督器：ollama socket 耗尽(Windows 偶发)自动重启续跑，直到回填正常退出
cd "E:/Graudate/gov-affair-kg-qa/repo"
for i in $(seq 1 40); do
  python -u scripts/backfill_vectors.py --batch-api --batch-size 64 --max-chars 1800 >> kg/import/checkpoints/backfill_full_run3.log 2>&1
  code=$?
  echo "[supervisor] round $i exit=$code $(date +%H:%M:%S)" >> kg/import/checkpoints/backfill_supervisor.log
  if [ $code -eq 0 ]; then echo "[supervisor] BACKFILL COMPLETE" >> kg/import/checkpoints/backfill_supervisor.log; exit 0; fi
  echo "[supervisor] restarting ollama..." >> kg/import/checkpoints/backfill_supervisor.log
  powershell -Command "Stop-Process -Name 'ollama*' -Force -ErrorAction SilentlyContinue" 2>/dev/null
  sleep 3
  cmd //c start "" "C:\Users\12492\AppData\Local\Programs\Ollama\ollama app.exe"
  for w in $(seq 1 12); do
    sleep 10
    curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
  done
  echo "[supervisor] ollama back up" >> kg/import/checkpoints/backfill_supervisor.log
done
echo "[supervisor] gave up after 40 rounds" >> kg/import/checkpoints/backfill_supervisor.log
exit 1
