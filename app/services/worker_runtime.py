from __future__ import annotations

from datetime import datetime, timedelta
import uuid
from typing import Any

from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from app.core.utils import ensure_utc, utc_now
from app.db.models import BacktestRun, WorkerCommand, WorkerRun, WorkerRunEvent
from app.services.runtime_control import RuntimeControlService

COMMAND_RUN_INGESTION = "run_ingestion_validation"
COMMAND_REFRESH_BARS = "refresh_bars"
COMMAND_RUN_LIVE_CYCLE = "run_live_cycle"
COMMAND_REFRESH_EARNINGS = "refresh_earnings_calendar"
COMMAND_RUN_BACKTEST = "run_backtest"

HIGH_PRIORITY_COMMAND_TYPES = [
    COMMAND_RUN_LIVE_CYCLE,
    COMMAND_REFRESH_BARS,
    COMMAND_RUN_INGESTION,
    COMMAND_REFRESH_EARNINGS,
]
LOW_PRIORITY_COMMAND_TYPES = [COMMAND_RUN_BACKTEST]

COMMAND_PRIORITY = {
    COMMAND_RUN_LIVE_CYCLE: 0,
    COMMAND_REFRESH_BARS: 1,
    COMMAND_RUN_INGESTION: 2,
    COMMAND_REFRESH_EARNINGS: 3,
    COMMAND_RUN_BACKTEST: 9,
}


class WorkerRuntimeService:
    ORPHANED_REASON = "worker restarted while task was running"

    def queue_command(
        self,
        session: Session,
        command_type: str,
        payload: dict[str, Any] | None = None,
        requested_by: str = "system",
    ) -> WorkerCommand:
        command = WorkerCommand(
            command_type=command_type,
            payload_json=payload or {},
            requested_by=requested_by,
            status="PENDING",
        )
        session.add(command)
        session.flush()
        return command

    def claim_next_command(
        self,
        session: Session,
        command_types: list[str] | None = None,
    ) -> WorkerCommand | None:
        priority_order = case(
            COMMAND_PRIORITY,
            value=WorkerCommand.command_type,
            else_=50,
        )
        stmt = (
            select(WorkerCommand)
            .where(WorkerCommand.status == "PENDING")
            .order_by(priority_order.asc(), WorkerCommand.created_at.asc(), WorkerCommand.id.asc())
            .limit(1)
        )
        if command_types:
            stmt = stmt.where(WorkerCommand.command_type.in_(command_types))
        command = session.execute(stmt).scalar_one_or_none()
        if command is None:
            return None
        command.status = "RUNNING"
        command.started_at = utc_now()
        session.flush()
        return command

    def complete_command(
        self,
        session: Session,
        command: WorkerCommand,
        result: dict[str, Any] | None = None,
    ) -> WorkerCommand:
        command.status = "COMPLETED"
        command.finished_at = utc_now()
        command.result_json = result or {}
        command.error_message = None
        session.flush()
        return command

    def fail_command(
        self,
        session: Session,
        command: WorkerCommand,
        error_message: str,
        result: dict[str, Any] | None = None,
    ) -> WorkerCommand:
        command.status = "FAILED"
        command.finished_at = utc_now()
        command.result_json = result or {}
        command.error_message = error_message[:4000]
        session.flush()
        return command

    def cancel_pending_commands(
        self,
        session: Session,
        *,
        command_types: list[str],
        reason: str,
        trigger: str | None = None,
    ) -> int:
        rows = session.execute(
            select(WorkerCommand)
            .where(
                WorkerCommand.status == "PENDING",
                WorkerCommand.command_type.in_(command_types),
            )
            .order_by(WorkerCommand.created_at.asc(), WorkerCommand.id.asc())
        ).scalars().all()
        if trigger is not None:
            rows = [
                row for row in rows
                if str((row.payload_json or {}).get("trigger") or "") == trigger
            ]
        now = utc_now()
        for row in rows:
            row.status = "CANCELLED"
            row.finished_at = now
            row.error_message = reason[:4000]
            result = dict(row.result_json or {})
            result.update({"cancelled": True, "reason": reason})
            row.result_json = result
        session.flush()
        return len(rows)

    def start_run(
        self,
        session: Session,
        run_type: str,
        trigger: str = "scheduled",
        run_key: str | None = None,
        **fields: Any,
    ) -> WorkerRun:
        run = WorkerRun(
            run_key=run_key or str(uuid.uuid4())[:8],
            run_type=run_type,
            trigger=trigger,
            status=str(fields.pop("status", "RUNNING")),
            stage=str(fields.pop("stage", "starting")),
            current_ticker=fields.pop("current_ticker", None),
            current_agent=fields.pop("current_agent", None),
            market_session=fields.pop("market_session", None),
            dry_run=bool(fields.pop("dry_run", False)),
            total_tickers=int(fields.pop("total_tickers", 0) or 0),
            completed_tickers=int(fields.pop("completed_tickers", 0) or 0),
            summary_json=fields.pop("summary_json", {}) or {},
            error_message=fields.pop("error_message", None),
            started_at=fields.pop("started_at", utc_now()),
            updated_at=utc_now(),
        )
        session.add(run)
        session.flush()
        return run

    def update_run(self, session: Session, run: WorkerRun, **fields: Any) -> WorkerRun:
        for key, value in fields.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.updated_at = utc_now()
        session.flush()
        return run

    def finish_run(
        self,
        session: Session,
        run: WorkerRun,
        status: str = "COMPLETED",
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
        **fields: Any,
    ) -> WorkerRun:
        if summary is not None:
            run.summary_json = summary
        run.status = status
        run.error_message = error_message
        run.finished_at = utc_now()
        for key, value in fields.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.updated_at = utc_now()
        session.flush()
        return run

    def add_event(
        self,
        session: Session,
        run_type: str,
        message: str,
        *,
        run: WorkerRun | None = None,
        level: str = "info",
        stage: str | None = None,
        ticker: str | None = None,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkerRunEvent:
        event = WorkerRunEvent(
            run_id=run.id if run else None,
            run_key=run.run_key if run else None,
            run_type=run_type,
            level=level,
            stage=stage,
            ticker=ticker,
            agent=agent,
            message=message,
            payload_json=payload or {},
        )
        session.add(event)
        session.flush()
        return event

    def latest_run(self, session: Session, run_type: str) -> WorkerRun | None:
        return session.execute(
            select(WorkerRun)
            .where(WorkerRun.run_type == run_type)
            .order_by(desc(WorkerRun.updated_at), desc(WorkerRun.id))
            .limit(1)
        ).scalar_one_or_none()

    def latest_running_run(self, session: Session, run_type: str) -> WorkerRun | None:
        return session.execute(
            select(WorkerRun)
            .where(
                WorkerRun.run_type == run_type,
                WorkerRun.status == "RUNNING",
            )
            .order_by(desc(WorkerRun.updated_at), desc(WorkerRun.id))
            .limit(1)
        ).scalar_one_or_none()

    def fail_run(
        self,
        session: Session,
        run: WorkerRun,
        *,
        reason: str,
        stage: str = "failed",
        summary_extra: dict[str, Any] | None = None,
    ) -> WorkerRun:
        summary = dict(run.summary_json or {})
        summary.setdefault("reconciled", True)
        summary["reason"] = reason
        if summary_extra:
            summary.update(summary_extra)
        run.summary_json = summary
        run.status = "FAILED"
        run.stage = stage
        run.finished_at = utc_now()
        run.updated_at = utc_now()
        run.error_message = reason[:4000]
        session.flush()
        return run

    def fail_stale_runs(
        self,
        session: Session,
        *,
        run_type: str,
        stale_after_seconds: int,
        reason: str,
    ) -> list[WorkerRun]:
        cutoff = utc_now() - timedelta(seconds=max(1, int(stale_after_seconds)))
        stale_runs = session.execute(
            select(WorkerRun)
            .where(
                WorkerRun.run_type == run_type,
                WorkerRun.status == "RUNNING",
                WorkerRun.updated_at < cutoff,
            )
            .order_by(WorkerRun.updated_at.asc(), WorkerRun.id.asc())
        ).scalars().all()
        for run in stale_runs:
            stale_for_seconds = max(
                0,
                int((utc_now() - ensure_utc(run.updated_at or run.started_at or utc_now())).total_seconds()),
            )
            self.fail_run(
                session,
                run,
                reason=reason,
                stage="failed_stale",
                summary_extra={"stale_for_seconds": stale_for_seconds},
            )
            self.add_event(
                session,
                run_type,
                f"Marked stale {run_type} run {run.run_key} as FAILED: {reason}",
                run=run,
                level="warn",
                stage="failed_stale",
                payload={"reason": reason, "stale_for_seconds": stale_for_seconds},
            )
        if stale_runs:
            session.flush()
        return stale_runs

    def recent_events(
        self,
        session: Session,
        run_types: list[str] | None = None,
        limit: int = 20,
    ) -> list[WorkerRunEvent]:
        stmt = select(WorkerRunEvent).order_by(desc(WorkerRunEvent.created_at), desc(WorkerRunEvent.id)).limit(limit)
        if run_types:
            stmt = stmt.where(WorkerRunEvent.run_type.in_(run_types))
        return session.execute(stmt).scalars().all()

    def recent_runs(
        self,
        session: Session,
        run_types: list[str] | None = None,
        limit: int = 12,
    ) -> list[WorkerRun]:
        stmt = select(WorkerRun).order_by(desc(WorkerRun.updated_at), desc(WorkerRun.id)).limit(limit)
        if run_types:
            stmt = stmt.where(WorkerRun.run_type.in_(run_types))
        return session.execute(stmt).scalars().all()

    def command_queue_snapshot(self, session: Session, limit: int = 8) -> dict[str, Any]:
        counts = {status: count for status, count in session.execute(
            select(WorkerCommand.status, func.count(WorkerCommand.id)).group_by(WorkerCommand.status)
        ).all()}
        rows = session.execute(
            select(WorkerCommand)
            .order_by(desc(WorkerCommand.created_at), desc(WorkerCommand.id))
            .limit(limit)
        ).scalars().all()
        return {
            "pending": int(counts.get("PENDING", 0)),
            "running": int(counts.get("RUNNING", 0)),
            "failed": int(counts.get("FAILED", 0)),
            "cancelled": int(counts.get("CANCELLED", 0)),
            "completed": int(counts.get("COMPLETED", 0)),
            "open": int(counts.get("PENDING", 0) + counts.get("RUNNING", 0)),
            "recent": [self._serialize_command(row) for row in rows],
        }

    def has_open_commands(self, session: Session, command_types: list[str]) -> bool:
        if not command_types:
            return False
        open_count = session.execute(
            select(func.count(WorkerCommand.id)).where(
                WorkerCommand.command_type.in_(command_types),
                WorkerCommand.status.in_(["PENDING", "RUNNING"]),
            )
        ).scalar_one()
        return int(open_count or 0) > 0

    def worker_status_snapshot(self, session: Session) -> dict[str, Any]:
        return {
            "worker": RuntimeControlService().get_worker_status(session),
            "supervisor": RuntimeControlService().get_supervisor_status(session),
            "command_queue": self.command_queue_snapshot(session),
        }

    def history_snapshot(
        self,
        session: Session,
        *,
        run_limit: int = 10,
        command_limit: int = 10,
        event_limit: int = 20,
    ) -> dict[str, Any]:
        runs = self.recent_runs(session, ["live_cycle", "bar_backfill", "backtest"], limit=run_limit)
        commands = session.execute(
            select(WorkerCommand)
            .order_by(desc(WorkerCommand.created_at), desc(WorkerCommand.id))
            .limit(command_limit)
        ).scalars().all()
        events = self.recent_events(session, ["live_cycle", "bar_backfill", "backtest"], limit=event_limit)
        return {
            "runs": [self._serialize_run_summary(run) for run in runs],
            "commands": [self._serialize_command(command) for command in commands],
            "events": [self._serialize_event(event) for event in events],
        }

    def runtime_snapshot(self, session: Session) -> dict[str, Any]:
        live_run = self.latest_run(session, "live_cycle")
        backfill_run = self.latest_run(session, "bar_backfill")
        backtest_run = self.latest_run(session, "backtest")
        events = list(reversed(self.recent_events(session, ["live_cycle", "bar_backfill", "backtest"], limit=16)))
        worker = RuntimeControlService().get_worker_status(session)
        supervisor = RuntimeControlService().get_supervisor_status(session)
        queue = self.command_queue_snapshot(session)

        timestamps = [
            value
            for value in [
                self._ensure_utc_dt(live_run.updated_at if live_run else None),
                self._ensure_utc_dt(backfill_run.updated_at if backfill_run else None),
                self._ensure_utc_dt(backtest_run.updated_at if backtest_run else None),
                self._ensure_utc_dt(events[-1].created_at if events else None),
                self._parse_iso_dt(worker.get("last_seen_at")),
                self._parse_iso_dt(supervisor.get("last_seen_at")),
            ]
            if value is not None
        ]
        updated_at = max(timestamps) if timestamps else utc_now()
        return {
            "updated_at": updated_at.isoformat(),
            "worker": worker,
            "supervisor": supervisor,
            "command_queue": queue,
            "live_cycle": self._serialize_run(live_run),
            "bar_backfill": self._serialize_run(backfill_run),
            "backtest": self._serialize_run(backtest_run),
            "recent_events": [self._serialize_event(event) for event in events],
        }

    def reconcile_orphaned_state(self, session: Session, reason: str | None = None) -> dict[str, int]:
        failure_reason = (reason or self.ORPHANED_REASON)[:4000]
        now = utc_now()
        counts = {
            "commands_failed": 0,
            "runs_failed": 0,
            "backtests_failed": 0,
        }

        commands = session.execute(
            select(WorkerCommand).where(WorkerCommand.status == "RUNNING")
        ).scalars().all()
        for command in commands:
            command.status = "FAILED"
            command.finished_at = now
            command.error_message = failure_reason
            result = dict(command.result_json or {})
            result.setdefault("reconciled", True)
            result.setdefault("reason", failure_reason)
            command.result_json = result
            counts["commands_failed"] += 1

        runs = session.execute(
            select(WorkerRun).where(WorkerRun.status == "RUNNING")
        ).scalars().all()
        for run in runs:
            run.status = "FAILED"
            run.stage = "failed"
            run.finished_at = now
            run.updated_at = now
            run.error_message = failure_reason
            summary = dict(run.summary_json or {})
            summary.setdefault("reconciled", True)
            summary.setdefault("reason", failure_reason)
            run.summary_json = summary
            counts["runs_failed"] += 1

        backtests = session.execute(
            select(BacktestRun).where(BacktestRun.status == "RUNNING")
        ).scalars().all()
        for run in backtests:
            metrics = dict(run.metrics or {})
            metrics.update(
                {
                    "phase": "failed",
                    "phase_label": "Failed",
                    "phase_detail": failure_reason,
                    "phase_pct": 100.0,
                    "error": failure_reason,
                    "last_progress_at": now.isoformat(),
                }
            )
            run.metrics = metrics
            run.status = "FAILED"
            run.finished_at = now
            counts["backtests_failed"] += 1

        if any(counts.values()):
            session.flush()
            session.commit()
        return counts

    def _serialize_run(self, run: WorkerRun | None) -> dict[str, Any]:
        if run is None:
            return {
                "cycle_id": None,
                "status": "idle",
                "stage": "idle",
                "current_ticker": None,
                "current_agent": None,
                "market_session": None,
                "dry_run": False,
                "total_tickers": 0,
                "completed_tickers": 0,
                "last_result": None,
                "error": None,
                "started_at": None,
                "updated_at": None,
                "finished_at": None,
                "mode": None,
                "skipped_reason": None,
                "run_mode": None,
                "event_driven_mode": None,
                "flow": {},
            }
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        flow: dict[str, Any] = {}
        for row in summary.get("results", []) if isinstance(summary, dict) else []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            flow_score = row.get("flow_score")
            position_multiplier = row.get("position_multiplier")
            used_cached_macro = row.get("used_cached_macro")
            used_cached_fundamentals = row.get("used_cached_fundamentals")
            if flow_score is None and position_multiplier is None and used_cached_macro is None and used_cached_fundamentals is None:
                continue
            flow[ticker] = {
                "flow_score": flow_score,
                "position_multiplier": position_multiplier,
                "used_cached_macro": used_cached_macro,
                "used_cached_fundamentals": used_cached_fundamentals,
            }
        return {
            "cycle_id": run.run_key,
            "status": run.status.lower(),
            "stage": run.stage,
            "current_ticker": run.current_ticker,
            "current_agent": run.current_agent,
            "market_session": run.market_session,
            "dry_run": run.dry_run,
            "total_tickers": run.total_tickers,
            "completed_tickers": run.completed_tickers,
            "last_result": summary or None,
            "error": run.error_message,
            "started_at": self._dt_to_iso(run.started_at),
            "updated_at": self._dt_to_iso(run.updated_at),
            "finished_at": self._dt_to_iso(run.finished_at),
            "mode": run.trigger,
            "skipped_reason": summary.get("reason"),
            "run_mode": summary.get("run_mode"),
            "event_driven_mode": summary.get("event_driven_mode"),
            "flow": flow,
        }

    def _serialize_event(self, event: WorkerRunEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "kind": event.run_type,
            "level": event.level,
            "stage": event.stage,
            "ticker": event.ticker,
            "agent": event.agent,
            "message": event.message,
            "payload": event.payload_json or {},
            "ts": self._dt_to_iso(event.created_at),
            "run_key": event.run_key,
        }

    def _serialize_command(self, command: WorkerCommand) -> dict[str, Any]:
        return {
            "id": command.id,
            "command_type": command.command_type,
            "status": command.status,
            "requested_by": command.requested_by,
            "created_at": self._dt_to_iso(command.created_at),
            "started_at": self._dt_to_iso(command.started_at),
            "finished_at": self._dt_to_iso(command.finished_at),
            "error": command.error_message,
        }

    def _serialize_run_summary(self, run: WorkerRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "run_key": run.run_key,
            "run_type": run.run_type,
            "trigger": run.trigger,
            "status": run.status,
            "stage": run.stage,
            "current_ticker": run.current_ticker,
            "current_agent": run.current_agent,
            "market_session": run.market_session,
            "dry_run": run.dry_run,
            "total_tickers": run.total_tickers,
            "completed_tickers": run.completed_tickers,
            "error": run.error_message,
            "summary": run.summary_json or {},
            "started_at": self._dt_to_iso(run.started_at),
            "updated_at": self._dt_to_iso(run.updated_at),
            "finished_at": self._dt_to_iso(run.finished_at),
        }

    def _ensure_utc_dt(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)

    def _dt_to_iso(self, value: datetime | None) -> str | None:
        normalized = self._ensure_utc_dt(value)
        return normalized.isoformat() if normalized is not None else None

    def _parse_iso_dt(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return ensure_utc(value)
        if not value or not isinstance(value, str):
            return None
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
