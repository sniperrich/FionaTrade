#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-6888}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-${CONDA_DEFAULT_ENV:-FionaTrade}}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
CONDA_BIN=""
DATABASE_URL_VALUE="${DATABASE_URL:-}"
PG_CTL_BIN=""
PG_ISREADY_BIN=""
PG_DATA_DIR=""
PG_HOST=""
PG_PORT=""
PG_STARTED_BY_SCRIPT=""

WEB_PID=""
SUPERVISOR_PID=""
RUN_CHILD_PID=""

log() {
  printf '[run_local] %s\n' "$*"
}

read_env_value() {
  local key="$1"
  if [[ -f "$ROOT_DIR/.env" ]]; then
    local line
    line="$(grep -E "^${key}=" "$ROOT_DIR/.env" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      printf '%s' "${line#*=}"
      return
    fi
  fi
  return 1
}

discover_pg_bin() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi
  local candidates=(
    "/opt/homebrew/opt/postgresql@16/bin/$name"
    "/opt/homebrew/opt/postgresql@17/bin/$name"
    "/usr/local/opt/postgresql@16/bin/$name"
    "/usr/local/opt/postgresql@17/bin/$name"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_postgres_ready() {
  if [[ -z "$DATABASE_URL_VALUE" ]]; then
    DATABASE_URL_VALUE="$(read_env_value DATABASE_URL || true)"
  fi
  if [[ -z "$DATABASE_URL_VALUE" ]]; then
    log "未发现 DATABASE_URL，沿用应用默认数据库配置。"
    return
  fi
  if [[ "$DATABASE_URL_VALUE" != postgres* ]]; then
    log "数据库后端: SQLite"
    return
  fi

  local stripped="$DATABASE_URL_VALUE"
  stripped="${stripped#postgresql+psycopg://}"
  stripped="${stripped#postgresql://}"
  stripped="${stripped#postgres://}"
  local authority="${stripped%%/*}"
  local hostport="${authority##*@}"
  PG_HOST="${hostport%%:*}"
  if [[ "$hostport" == *:* ]]; then
    PG_PORT="${hostport##*:}"
  else
    PG_PORT="5432"
  fi
  local db_name="${stripped#*/}"
  db_name="${db_name%%\?*}"

  if [[ "$PG_HOST" != "127.0.0.1" && "$PG_HOST" != "localhost" ]]; then
    log "数据库后端: PostgreSQL (${PG_HOST}:${PG_PORT}/${db_name})，由外部服务提供。"
    return
  fi

  PG_ISREADY_BIN="$(discover_pg_bin pg_isready || true)"
  PG_CTL_BIN="$(discover_pg_bin pg_ctl || true)"
  if [[ -z "$PG_ISREADY_BIN" || -z "$PG_CTL_BIN" ]]; then
    log "DATABASE_URL 指向 PostgreSQL，但未找到 pg_isready/pg_ctl。请先安装 PostgreSQL。"
    exit 1
  fi

  if "$PG_ISREADY_BIN" -h "$PG_HOST" -p "$PG_PORT" >/dev/null 2>&1; then
    log "数据库后端: PostgreSQL (${PG_HOST}:${PG_PORT}/${db_name})，已在线。"
    return
  fi

  local candidates=(
    "${PGDATA:-}"
    "/opt/homebrew/var/postgresql@16"
    "/opt/homebrew/var/postgresql@17"
    "/usr/local/var/postgresql@16"
    "/usr/local/var/postgresql@17"
  )
  local candidate=""
  for candidate in "${candidates[@]}"; do
    if [[ -n "$candidate" && -d "$candidate" && -f "$candidate/PG_VERSION" ]]; then
      PG_DATA_DIR="$candidate"
      break
    fi
  done

  if [[ -z "$PG_DATA_DIR" ]]; then
    log "DATABASE_URL 指向本地 PostgreSQL，但未找到可用 PGDATA。请先 initdb 或设置 PGDATA。"
    exit 1
  fi

  mkdir -p "$LOG_DIR"
  log "正在启动本地 PostgreSQL: host=${PG_HOST} port=${PG_PORT} data=${PG_DATA_DIR}"
  "$PG_CTL_BIN" -D "$PG_DATA_DIR" -l "$LOG_DIR/postgres.local.log" -o "-p ${PG_PORT}" start >/dev/null
  PG_STARTED_BY_SCRIPT="1"

  local attempt
  for attempt in {1..15}; do
    if "$PG_ISREADY_BIN" -h "$PG_HOST" -p "$PG_PORT" >/dev/null 2>&1; then
      log "PostgreSQL 已就绪: ${PG_HOST}:${PG_PORT}/${db_name}"
      return
    fi
    sleep 1
  done

  log "PostgreSQL 启动超时，请查看 ${LOG_DIR}/postgres.local.log"
  exit 1
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
  if [[ -n "$PG_STARTED_BY_SCRIPT" && -n "$PG_CTL_BIN" && -n "$PG_DATA_DIR" ]]; then
    "$PG_CTL_BIN" -D "$PG_DATA_DIR" stop -m fast >/dev/null 2>&1 || true
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

ensure_postgres_ready

run_in_env web env FIONA_WEB_HOST="127.0.0.1" FIONA_WEB_PORT="$PORT" uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
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
