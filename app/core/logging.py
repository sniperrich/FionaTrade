from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP_LOGGER_NAME = "fionatrade.app"
WRITEOUT_LOGGER_NAME = "fionatrade.writeout"
HEALTH_LOGGER_NAME = "fionatrade.health"
LIVE_TRADING_LOGGER_NAME = "fionatrade.live_trading"
AGENTS_LOGGER_NAME = "fionatrade.agents"
BAR_REFRESH_LOGGER_NAME = "fionatrade.bar_refresh"

DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_LOGGING_READY = False
_LOGGING_LOCK = threading.Lock()

# Production settings: 50 MB per file, keep 10 rotated files (~500 MB total per log stream)
_MAX_BYTES = 50_000_000
_BACKUP_COUNT = 10


def _resolve_level(log_level: str) -> int:
    return getattr(logging, log_level.upper(), logging.INFO)


def _replace_handlers(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    for handler in handlers:
        logger.addHandler(handler)


def _rotating_handler(
    path: Path,
    level: int,
    fmt: str,
    max_bytes: int = _MAX_BYTES,
    backup_count: int = _BACKUP_COUNT,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    return handler


def setup_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    global _LOGGING_READY
    level = _resolve_level(log_level)
    output_dir = Path(log_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Console: INFO+ with clean format ───────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))

    # ── app.log: all logs (50 MB, 10 backups) ──────────────────────────────
    app_file_handler = _rotating_handler(output_dir / "app.log", level, DEFAULT_FORMAT)

    # ── errors.log: ERROR+ only, catches everything across all loggers ─────
    error_handler = _rotating_handler(
        output_dir / "errors.log",
        logging.ERROR,
        DEFAULT_FORMAT,
        max_bytes=20_000_000,
        backup_count=5,
    )

    root = logging.getLogger()
    root.setLevel(level)
    _replace_handlers(root, [console_handler, app_file_handler, error_handler])

    # ── writeout.log: pipeline tick summaries (JSON) ───────────────────────
    writeout_logger = logging.getLogger(WRITEOUT_LOGGER_NAME)
    writeout_logger.setLevel(level)
    writeout_logger.propagate = False
    _replace_handlers(
        writeout_logger,
        [_rotating_handler(output_dir / "writeout.log", level, "%(message)s")],
    )

    # ── health.log: health audit snapshots ─────────────────────────────────
    health_logger = logging.getLogger(HEALTH_LOGGER_NAME)
    health_logger.setLevel(level)
    health_logger.propagate = False
    _replace_handlers(
        health_logger,
        [_rotating_handler(output_dir / "health.log", level, "%(message)s")],
    )

    # ── live_trading.log: every cycle decision (JSON, structured) ──────────
    live_logger = logging.getLogger(LIVE_TRADING_LOGGER_NAME)
    live_logger.setLevel(level)
    live_logger.propagate = True  # also goes to app.log
    _replace_handlers(
        live_logger,
        [_rotating_handler(output_dir / "live_trading.log", level, "%(message)s")],
    )

    # ── agents.log: per-AgentRun structured decisions (JSON) ───────────────
    agents_logger = logging.getLogger(AGENTS_LOGGER_NAME)
    agents_logger.setLevel(level)
    agents_logger.propagate = True  # also goes to app.log
    _replace_handlers(
        agents_logger,
        [_rotating_handler(output_dir / "agents.log", level, "%(message)s")],
    )

    # ── bar_refresh.log: K-line fetch status ───────────────────────────────
    bar_logger = logging.getLogger(BAR_REFRESH_LOGGER_NAME)
    bar_logger.setLevel(level)
    bar_logger.propagate = True  # also goes to app.log
    _replace_handlers(
        bar_logger,
        [
            _rotating_handler(
                output_dir / "bar_refresh.log",
                level,
                "%(message)s",
                max_bytes=10_000_000,
                backup_count=5,
            )
        ],
    )

    _LOGGING_READY = True


def ensure_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    with _LOGGING_LOCK:
        if _LOGGING_READY:
            return
        setup_logging(log_dir=log_dir, log_level=log_level)


def get_app_logger() -> logging.Logger:
    return logging.getLogger(APP_LOGGER_NAME)


def get_writeout_logger() -> logging.Logger:
    return logging.getLogger(WRITEOUT_LOGGER_NAME)


def get_health_logger() -> logging.Logger:
    return logging.getLogger(HEALTH_LOGGER_NAME)


def get_live_trading_logger() -> logging.Logger:
    return logging.getLogger(LIVE_TRADING_LOGGER_NAME)


def get_agents_logger() -> logging.Logger:
    return logging.getLogger(AGENTS_LOGGER_NAME)


def get_bar_refresh_logger() -> logging.Logger:
    return logging.getLogger(BAR_REFRESH_LOGGER_NAME)


def _json_line(event: str, payload: dict[str, Any]) -> str:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    return json.dumps(record, ensure_ascii=False, default=str)


def log_writeout(event: str, payload: dict[str, Any]) -> None:
    get_writeout_logger().info(_json_line(event, payload))


def log_health(event: str, payload: dict[str, Any]) -> None:
    get_health_logger().info(_json_line(event, payload))


def log_live_cycle(cycle_id: str, payload: dict[str, Any]) -> None:
    """Log one live-trading cycle result as a structured JSON line to live_trading.log."""
    get_live_trading_logger().info(_json_line("live_cycle", {"cycle_id": cycle_id, **payload}))


def log_agent_run(ticker: str, payload: dict[str, Any]) -> None:
    """Log one AgentRun decision as a structured JSON line to agents.log.

    Recommended payload keys:
      final_action, final_position_pct, final_reasoning,
      news_signal, news_confidence, news_catalyst,
      macro_signal, fundamentals_signal, technicals_signal,
      risk_approved, execution_ms
    """
    get_agents_logger().info(_json_line("agent_run", {"ticker": ticker, **payload}))


def log_bar_refresh(event: str, payload: dict[str, Any]) -> None:
    """Log a bar-refresh operation result to bar_refresh.log."""
    get_bar_refresh_logger().info(_json_line(event, payload))
