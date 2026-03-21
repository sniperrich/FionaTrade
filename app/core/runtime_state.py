from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_live_state() -> dict[str, Any]:
    now = _now_iso()
    return {
        "updated_at": now,
        "live_cycle": {
            "status": "idle",
            "cycle_id": None,
            "stage": "idle",
            "current_ticker": None,
            "current_agent": None,
            "started_at": None,
            "updated_at": now,
            "completed_tickers": 0,
            "total_tickers": 0,
            "last_result": None,
            "error": None,
        },
        "bar_backfill": {
            "status": "idle",
            "mode": None,
            "stage": "idle",
            "current_ticker": None,
            "started_at": None,
            "updated_at": now,
            "completed_tickers": 0,
            "total_tickers": 0,
            "result": None,
            "error": None,
        },
        "recent_events": [],
    }


class RuntimeStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._state: dict[str, Any] = _default_live_state()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=40)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = deepcopy(self._state)
            payload["recent_events"] = list(self._recent_events)
            return payload

    def patch_section(self, section: str, **updates: Any) -> None:
        with self._lock:
            section_state = self._state.setdefault(section, {})
            section_state.update(updates)
            section_state["updated_at"] = _now_iso()
            self._state["updated_at"] = section_state["updated_at"]

    def push_event(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        event = {
            "ts": _now_iso(),
            "kind": kind,
            "level": level,
            "message": message,
            **data,
        }
        with self._lock:
            self._recent_events.appendleft(event)
            self._state["updated_at"] = event["ts"]

    def reset(self) -> None:
        with self._lock:
            self._state = _default_live_state()
            self._recent_events.clear()


_LIVE_RUNTIME = RuntimeStateStore()


def get_live_runtime_state() -> dict[str, Any]:
    return _LIVE_RUNTIME.snapshot()


def patch_live_runtime(section: str, **updates: Any) -> None:
    _LIVE_RUNTIME.patch_section(section, **updates)


def push_live_event(kind: str, message: str, level: str = "info", **data: Any) -> None:
    _LIVE_RUNTIME.push_event(kind=kind, message=message, level=level, **data)


def reset_live_runtime_state() -> None:
    _LIVE_RUNTIME.reset()
