from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_health, log_writeout
from app.core.utils import ensure_utc, utc_now
from app.db.models import Event, RawItem, Signal
from app.paper_engine.service import PaperEngineService


class HealthAuditService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.paper_engine = PaperEngineService(settings)

    def snapshot(self, session: Session, scheduler_running: bool | None = None) -> dict:
        now = utc_now()
        db_ok = False
        issues: list[str] = []

        try:
            session.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            issues.append("db_query_failed")

        unprocessed_raw = session.execute(
            select(func.count(RawItem.id)).where(RawItem.processed.is_(False))
        ).scalar_one_or_none() or 0

        latest_ingested = session.execute(select(func.max(RawItem.ingested_at))).scalar_one_or_none()
        source_latency_sec = None
        if latest_ingested:
            source_latency_sec = round((now - ensure_utc(latest_ingested)).total_seconds(), 2)

        active_signals = session.execute(
            select(func.count(Signal.id)).where(and_(Signal.status == "ACTIVE", Signal.expires_at > now))
        ).scalar_one_or_none() or 0

        one_day_ago = now - timedelta(days=1)
        watch_events_24h = session.execute(
            select(func.count(Event.id)).where(and_(Event.validation_status == "WATCH", Event.created_at >= one_day_ago))
        ).scalar_one_or_none() or 0

        rejected_risk_24h = session.execute(
            select(func.count(Signal.id)).where(and_(Signal.status == "REJECTED_RISK", Signal.created_at >= one_day_ago))
        ).scalar_one_or_none() or 0

        portfolio = self.paper_engine.portfolio(session)
        nav = float(portfolio.get("nav", 0.0))

        if source_latency_sec is not None and source_latency_sec > self.settings.poll_interval_seconds * 3:
            issues.append("source_latency_high")
        if unprocessed_raw > 2000:
            issues.append("raw_backlog_high")
        if nav <= 0:
            issues.append("nav_non_positive")

        status = "ok" if not issues else "warn"
        return {
            "status": status,
            "checked_at": now.isoformat(),
            "db_ok": db_ok,
            "scheduler_running": scheduler_running,
            "source_latency_sec": source_latency_sec,
            "unprocessed_raw": int(unprocessed_raw),
            "active_signals": int(active_signals),
            "watch_events_24h": int(watch_events_24h),
            "rejected_risk_24h": int(rejected_risk_24h),
            "nav": nav,
            "issues": issues,
        }

    def run_and_log(self, session: Session, scheduler_running: bool | None = None) -> dict:
        snapshot = self.snapshot(session, scheduler_running=scheduler_running)
        log_health("health_audit", snapshot)
        log_writeout("health_audit", snapshot)
        return snapshot
