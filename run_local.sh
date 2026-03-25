#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-6888}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-${CONDA_DEFAULT_ENV:-FionaTrade}}"

WEB_PID=""
SUPERVISOR_PID=""

log() {
  printf '[run_local] %s\n' "$*"
}

require_conda() {
  if ! command -v conda >/dev/null 2>&1; then
    log "未找到 conda，请先安装并确保 conda 在 PATH 中。"
    exit 1
  fi
}

run_in_env() {
  local label="$1"
  shift

  if [[ -n "${CONDA_PREFIX:-}" ]]; then
    (
      cd "$ROOT_DIR"
      exec "$@"
    ) > >(sed "s/^/[$label] /") 2> >(sed "s/^/[$label] /" >&2) &
  else
    require_conda
    (
      cd "$ROOT_DIR"
      exec conda run --no-capture-output -n "$CONDA_ENV_NAME" "$@"
    ) > >(sed "s/^/[$label] /") 2> >(sed "s/^/[$label] /" >&2) &
  fi
  echo $!
}

cleanup() {
  local code=$?
  trap - EXIT INT TERM

  if [[ -n "$WEB_PID" ]] && kill -0 "$WEB_PID" >/dev/null 2>&1; then
    kill "$WEB_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SUPERVISOR_PID" ]] && kill -0 "$SUPERVISOR_PID" >/dev/null 2>&1; then
    kill "$SUPERVISOR_PID" >/dev/null 2>&1 || true
  fi
  wait >/dev/null 2>&1 || true
  exit "$code"
}

trap cleanup EXIT INT TERM

log "项目目录: $ROOT_DIR"
if [[ -n "${CONDA_PREFIX:-}" ]]; then
  log "使用当前已激活 conda 环境: ${CONDA_DEFAULT_ENV:-unknown}"
else
  log "使用 conda 环境: $CONDA_ENV_NAME"
fi

WEB_PID="$(run_in_env web uvicorn app.main:app --host "$HOST" --port "$PORT" --reload)"
log "Web 已启动，PID=$WEB_PID，地址: http://127.0.0.1:$PORT"

SUPERVISOR_PID="$(run_in_env supervisor python -m app.worker.supervisor)"
log "Worker supervisor 已启动，PID=$SUPERVISOR_PID"

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
