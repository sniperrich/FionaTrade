#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-6888}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-${CONDA_DEFAULT_ENV:-FionaTrade}}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
CONDA_BIN=""

WEB_PID=""
SUPERVISOR_PID=""
RUN_CHILD_PID=""

log() {
  printf '[run_local] %s\n' "$*"
}

require_conda() {
  if [[ -n "$CONDA_BIN" ]]; then
    return
  fi
  if command -v conda >/dev/null 2>&1; then
    CONDA_BIN="$(command -v conda)"
    return
  fi
  local candidates=(
    "$HOME/miniconda3/bin/conda"
    "$HOME/anaconda3/bin/conda"
    "/opt/homebrew/Caskroom/miniconda/base/bin/conda"
  )
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      CONDA_BIN="$candidate"
      return
    fi
  done
  log "未找到 conda，请先安装并确保 conda 在 PATH 中，或设置 CONDA_BIN。"
  exit 1
}

run_in_env() {
  local label="$1"
  shift
  mkdir -p "$LOG_DIR"
  local log_file="$LOG_DIR/${label}.local.log"

  if [[ -n "${CONDA_PREFIX:-}" ]]; then
    (
      cd "$ROOT_DIR"
      exec "$@"
    ) > >(tee -a "$log_file" | sed "s/^/[$label] /") 2> >(tee -a "$log_file" | sed "s/^/[$label] /" >&2) &
  else
    require_conda
    (
      cd "$ROOT_DIR"
      exec "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV_NAME" "$@"
    ) > >(tee -a "$log_file" | sed "s/^/[$label] /") 2> >(tee -a "$log_file" | sed "s/^/[$label] /" >&2) &
  fi
  RUN_CHILD_PID="$!"
}

stop_tree() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return
  fi
  pkill -TERM -P "$pid" >/dev/null 2>&1 || true
  kill "$pid" >/dev/null 2>&1 || true
  sleep 1
  pkill -KILL -P "$pid" >/dev/null 2>&1 || true
  kill -KILL "$pid" >/dev/null 2>&1 || true
}

cleanup() {
  local code=$?
  trap - EXIT INT TERM

  if [[ -n "$WEB_PID" ]] && kill -0 "$WEB_PID" >/dev/null 2>&1; then
    stop_tree "$WEB_PID"
  fi
  if [[ -n "$SUPERVISOR_PID" ]] && kill -0 "$SUPERVISOR_PID" >/dev/null 2>&1; then
    stop_tree "$SUPERVISOR_PID"
  fi
  wait >/dev/null 2>&1 || true
  exit "$code"
}

ensure_alive() {
  local label="$1"
  local pid="$2"
  local grace_seconds="${3:-3}"
  sleep "$grace_seconds"
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    log "$label 启动失败，进程在 ${grace_seconds}s 内退出。请查看 $LOG_DIR/${label}.local.log"
    exit 1
  fi
}

trap cleanup EXIT INT TERM

log "项目目录: $ROOT_DIR"
if [[ -n "${CONDA_PREFIX:-}" ]]; then
  log "使用当前已激活 conda 环境: ${CONDA_DEFAULT_ENV:-unknown}"
else
  require_conda
  log "使用 conda 环境: $CONDA_ENV_NAME ($CONDA_BIN)"
fi

run_in_env web uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
WEB_PID="$RUN_CHILD_PID"
log "Web 已启动，PID=${WEB_PID}，地址: http://127.0.0.1:${PORT}"
ensure_alive web "$WEB_PID" 3

run_in_env supervisor python -m app.worker.supervisor
SUPERVISOR_PID="$RUN_CHILD_PID"
log "Worker supervisor 已启动，PID=${SUPERVISOR_PID}"
ensure_alive supervisor "$SUPERVISOR_PID" 3

log "日志文件: ${LOG_DIR}/web.local.log / ${LOG_DIR}/supervisor.local.log"
log "按 Ctrl+C 可同时停止 web 和 supervisor。"

while true; do
  if ! kill -0 "$WEB_PID" >/dev/null 2>&1; then
    log "Web 进程已退出，准备关闭 supervisor。"
    break
  fi
  if ! kill -0 "$SUPERVISOR_PID" >/dev/null 2>&1; then
    log "Supervisor 进程已退出，准备关闭 web。"
    break
  fi
  sleep 1
done
