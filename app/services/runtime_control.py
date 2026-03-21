from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import RuntimeControl

CONTROL_LIVE_ENABLED = "live_trading_enabled"


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
    ) -> bool:
        self.set(
            session,
            CONTROL_LIVE_ENABLED,
            {
                "enabled": bool(enabled),
                "source": source,
                "env_default": bool(settings.live_trading_enabled),
            },
        )
        return bool(enabled)
