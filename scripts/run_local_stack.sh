#!/usr/bin/env bash
# The demo stack from the Windows venv (Git Bash): API :8000, N batch workers, the GPU tunnel; Postgres and Redis stay in Docker
# (ports 5433 / 6380, started with `docker compose up -d postgres redis` in WSL). Why not the containers: inside Docker, `localhost` is the
# container, and on this machine neither host.docker.internal nor the WSL gateway reaches an ssh tunnel on the Windows side.
#
#   scripts/run_local_stack.sh start [workers=3]     scripts/run_local_stack.sh stop
#
# Settings (see app/config.py): READER_PAGE_ENGINES picks and orders the page engines (the first is primary); READER_PAGE_HALVES=1 adds the upper
# and lower half reads (+2 fields on DEV, ~2.5x the time per page).
set -u
cd "$(dirname "$0")/.."
PY=./.venv/Scripts/python.exe
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://idp:idp@localhost:5433/idp_pipeline}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6380/0}"
export READER_ENGINES_FILE="${READER_ENGINES_FILE:-config/reader_engines.json}"
export READER_PAGE_ENGINES="${READER_PAGE_ENGINES:-page_qwen3vl,page_glmocr}"
export READER_PAGE_HALVES="${READER_PAGE_HALVES:-0}"
LOGDIR="${LOGDIR:-/tmp}"

case "${1:-start}" in
  start)
    N="${2:-3}"
    BIND=127.0.0.1 nohup scripts/gpu_tunnel.sh 11434 > "$LOGDIR/idp_tunnel.log" 2>&1 &
    $PY -m alembic upgrade head > "$LOGDIR/idp_migrate.log" 2>&1
    nohup $PY -m uvicorn app.main:app --port 8000 > "$LOGDIR/idp_api.log" 2>&1 &
    for i in $(seq 1 "$N"); do nohup $PY -m app.worker > "$LOGDIR/idp_worker$i.log" 2>&1 & done
    echo "started: tunnel, API on :8000, $N workers (logs in $LOGDIR/idp_*.log)"
    ;;
  stop)
    powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*app.worker*' -or \$_.CommandLine -like '*uvicorn app.main*' -or \$_.CommandLine -like '*gpu_tunnel.sh*' -or (\$_.Name -eq 'ssh.exe' -and \$_.CommandLine -like '*-N -o BatchMode*') } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }"
    echo stopped
    ;;
  *) echo "usage: $0 start [workers] | stop"; exit 1;;
esac
