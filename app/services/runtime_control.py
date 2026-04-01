from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import RuntimeControl

CONTROL_LIVE_ENABLED = "live_trading_enabled"
CONTROL_OVERNIGHT_RISK_STATE = "overnight_risk_state"
CONTROL_WORKER_HEARTBEAT = "worker_heartbeat"
CONTROL_WORKER_SUPERVISOR = "worker_supervisor"
DEFAULT_WORKER_STALE_SECONDS = 20


class RuntimeControlService:
    def get(self, session: Session, key: str) -> dict[str, Any] | None:
        row = session.execute(
            select(RuntimeControl).where(RuntimeControl.control_key == key).limit(1)
        ).scalar_one_or_none()
        return row.value_json if row else None

    def set(self, session: Session, key: str, value: dict[str, Any]) -> dict[str, Any]:
        row = session.execute(
            select(RuntimeControl).where(RuntimeControl.control_key == key).limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = RuntimeControl(control_key=key, value_json=value)
            session.add(row)
        else:
            row.value_json = value
        session.flush()
        return row.value_json

    def get_live_enabled(self, session: Session, settings: Settings) -> bool:
        row = self.get(session, CONTROL_LIVE_ENABLED)
        if row is None:
            return bool(settings.live_trading_enabled)
        return bool(row.get("enabled", False))

    def set_live_enabled(
        self,
        session: Session,
        settings: Settings,
        enabled: bool,
        source: str = "api",
        disable_mode: str | None = None,
    ) -> bool:
        current = self.get(session, CONTROL_LIVE_ENABLED) or {}
        now = utc_now()
        payload = {
            "enabled": bool(enabled),
            "source": source,
            "env_default": bool(settings.live_trading_enabled),
            "last_disable_mode": (
                str(disable_mode or current.get("last_disable_mode") or settings.live_disable_default_mode).upper()
                if not enabled else current.get("last_disable_mode")
            ),
        }
        if enabled:
            if not bool(current.get("enabled", False)):
                warmup_minutes = max(0, int(getattr(settings, "live_enable_warmup_minutes", 15) or 0))
                payload["enabled_at"] = now.isoformat()
                payload["warmup_until"] = (now + timedelta(minutes=warmup_minutes)).isoformat()
            else:
                payload["enabled_at"] = current.get("enabled_at")
                payload["warmup_until"] = current.get("warmup_until")
            payload["disabled_at"] = current.get("disabled_at")
        else:
            payload["enabled_at"] = current.get("enabled_at")
            payload["warmup_until"] = current.get("warmup_until")
            payload["disabled_at"] = now.isoformat()
        self.set(
            session,
            CONTROL_LIVE_ENABLED,
            payload,
        )
        return bool(enabled)

    def touch_worker_heartbeat(
        self,
        session: Session,
        *,
        pid: int,
        started_at: datetime,
        source: str = "worker",
        scheduler_running: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._touch_process_heartbeat(
            session,
            key=CONTROL_WORKER_HEARTBEAT,
            pid=pid,
            started_at=started_at,
            source=source,
            scheduler_running=scheduler_running,
            extra=extra,
        )

    def mark_worker_offline(
        self,
        session: Session,
        *,
        pid: int | None = None,
        source: str = "worker",
        reason: str = "shutdown",
    ) -> dict[str, Any]:
        return self._mark_process_offline(
            session,
            key=CONTROL_WORKER_HEARTBEAT,
            pid=pid,
            source=source,
            reason=reason,
        )

    def touch_supervisor_heartbeat(
        self,
        session: Session,
        *,
        pid: int,
        started_at: datetime,
        source: str = "worker_supervisor",
        scheduler_running: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._touch_process_heartbeat(
            session,
            key=CONTROL_WORKER_SUPERVISOR,
            pid=pid,
            started_at=started_at,
            source=source,
            scheduler_running=scheduler_running,
            extra=extra,
        )

    def mark_supervisor_offline(
        self,
        session: Session,
        *,
        pid: int | None = None,
        source: str = "worker_supervisor",
        reason: str = "shutdown",
    ) -> dict[str, Any]:
        return self._mark_process_offline(
            session,
            key=CONTROL_WORKER_SUPERVISOR,
            pid=pid,
            source=source,
            reason=reason,
        )

    def _mark_process_offline(
        self,
        session: Session,
        *,
        key: str,
        pid: int | None = None,
        source: str,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        payload = self.get(session, key) or {}
        if pid is not None:
            payload["pid"] = int(pid)
        payload.update(
            {
                "source": source,
                "status": "OFFLINE",
                "last_seen_at": now.isoformat(),
                "stopped_at": now.isoformat(),
                "reason": reason,
            }
        )
        self.set(session, key, payload)
        return payload

    def get_worker_status(
        self,
        session: Session,
        stale_after_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    ) -> dict[str, Any]:
        return self._get_process_status(session, CONTROL_WORKER_HEARTBEAT, stale_after_seconds)

    def get_supervisor_status(
        self,
        session: Session,
        stale_after_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    ) -> dict[str, Any]:
        return self._get_process_status(session, CONTROL_WORKER_SUPERVISOR, stale_after_seconds)

    def _touch_process_heartbeat(
        self,
        session: Session,
        *,
        key: str,
        pid: int,
        started_at: datetime,
        source: str,
        scheduler_running: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        payload = self.get(session, key) or {}
        payload.update(
            {
                "pid": int(pid),
                "source": source,
                "status": "ONLINE",
                "started_at": ensure_utc(started_at).isoformat(),
                "last_seen_at": now.isoformat(),
                "scheduler_running": bool(scheduler_running),
            }
        )
        if extra:
            payload.update(extra)
        self.set(session, key, payload)
        return payload

    def _get_process_status(
        self,
        session: Session,
        key: str,
        stale_after_seconds: int,
    ) -> dict[str, Any]:
        payload = self.get(session, key) or {}
        started_at = self._parse_dt(payload.get("started_at"))
        last_seen_at = self._parse_dt(payload.get("last_seen_at"))
        stopped_at = self._parse_dt(payload.get("stopped_at"))
        now = utc_now()
        age_seconds = None
        if last_seen_at is not None:
            age_seconds = round((now - last_seen_at).total_seconds(), 1)

        raw_status = str(payload.get("status", "")).upper()
        if raw_status == "OFFLINE":
            status = "OFFLINE"
            online = False
            reason = payload.get("reason") or "worker stopped"
        elif last_seen_at is None:
            status = "OFFLINE"
            online = False
            reason = "no heartbeat recorded"
        elif age_seconds is not None and age_seconds <= stale_after_seconds:
            status = "ONLINE"
            online = True
            reason = None
        else:
            status = "STALE"
            online = False
            reason = f"last heartbeat {age_seconds:.1f}s ago" if age_seconds is not None else "heartbeat missing"

        return {
            "status": status,
            "online": online,
            "pid": payload.get("pid"),
            "source": payload.get("source") or "worker",
            "scheduler_running": bool(payload.get("scheduler_running", False)),
            "started_at": started_at.isoformat() if started_at else None,
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
            "stopped_at": stopped_at.isoformat() if stopped_at else None,
            "last_seen_age_seconds": age_seconds,
            "reason": reason,
            "live_enabled": bool(payload.get("live_enabled", False)),
            "configured_tickers": payload.get("configured_tickers") or [],
            "host": payload.get("host"),
        }

    def _parse_dt(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return ensure_utc(value)
        if not value or not isinstance(value, str):
            return None
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
