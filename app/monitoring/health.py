from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_health, log_writeout
from app.core.utils import ensure_utc, utc_now
from app.db.models import AgentRun, LiveTrade, RawItem, SourceStatus
from app.services.runtime_control import RuntimeControlService


class HealthAuditService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime_control = RuntimeControlService()

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
        source_rows = session.execute(select(SourceStatus)).scalars().all()
        sources = {
            row.source_key: {
                "status": row.status,
                "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "error_message": row.error_message,
            }
            for row in source_rows
        }

        one_day_ago = now - timedelta(days=1)
        agent_runs_24h = session.execute(
            select(func.count(AgentRun.id)).where(AgentRun.created_at >= one_day_ago)
        ).scalar_one_or_none() or 0
        live_trades_24h = session.execute(
            select(func.count(LiveTrade.id)).where(LiveTrade.created_at >= one_day_ago)
        ).scalar_one_or_none() or 0
        worker = self.runtime_control.get_worker_status(session)
        supervisor = self.runtime_control.get_supervisor_status(session)

        if source_latency_sec is not None and source_latency_sec > self.settings.poll_interval_seconds * 3:
            issues.append("source_latency_high")
        if unprocessed_raw > 2000:
            issues.append("raw_backlog_high")
        if worker["status"] == "OFFLINE":
            issues.append("worker_offline")
        elif worker["status"] == "STALE":
            issues.append("worker_stale")

        status = "ok" if not issues else "warn"
        return {
            "status": status,
            "checked_at": now.isoformat(),
            "db_ok": db_ok,
            "scheduler_running": scheduler_running,
            "source_latency_sec": source_latency_sec,
            "sources": sources,
            "unprocessed_raw": int(unprocessed_raw),
            "agent_runs_24h": int(agent_runs_24h),
            "live_trades_24h": int(live_trades_24h),
            "worker": worker,
            "supervisor": supervisor,
            "issues": issues,
        }

    def run_and_log(self, session: Session, scheduler_running: bool | None = None) -> dict:
        snapshot = self.snapshot(session, scheduler_running=scheduler_running)
        log_health("health_audit", snapshot)
        log_writeout("health_audit", snapshot)
        return snapshot
