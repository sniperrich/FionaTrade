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

DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_LOGGING_READY = False
_LOGGING_LOCK = threading.Lock()


def _resolve_level(log_level: str) -> int:
    return getattr(logging, log_level.upper(), logging.INFO)


def _replace_handlers(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    for handler in handlers:
        logger.addHandler(handler)


def _rotating_handler(path: Path, level: int, fmt: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    return handler


def setup_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    global _LOGGING_READY
    level = _resolve_level(log_level)
    output_dir = Path(log_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))

    app_file_handler = _rotating_handler(output_dir / "app.log", level, DEFAULT_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    _replace_handlers(root, [console_handler, app_file_handler])

    writeout_logger = logging.getLogger(WRITEOUT_LOGGER_NAME)
    writeout_logger.setLevel(level)
    writeout_logger.propagate = False
    _replace_handlers(
        writeout_logger,
        [_rotating_handler(output_dir / "writeout.log", level, "%(message)s")],
    )

    health_logger = logging.getLogger(HEALTH_LOGGER_NAME)
    health_logger.setLevel(level)
    health_logger.propagate = False
    _replace_handlers(
        health_logger,
        [_rotating_handler(output_dir / "health.log", level, "%(message)s")],
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
