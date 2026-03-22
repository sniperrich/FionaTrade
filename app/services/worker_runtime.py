from __future__ import annotations

from datetime import datetime
import uuid
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.utils import utc_now
from app.db.models import WorkerCommand, WorkerRun, WorkerRunEvent
from app.services.runtime_control import RuntimeControlService

COMMAND_RUN_INGESTION = "run_ingestion_validation"
COMMAND_REFRESH_BARS = "refresh_bars"
COMMAND_RUN_LIVE_CYCLE = "run_live_cycle"
COMMAND_REFRESH_EARNINGS = "refresh_earnings_calendar"


class WorkerRuntimeService:
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
        stmt = (
            select(WorkerCommand)
            .where(WorkerCommand.status == "PENDING")
            .order_by(WorkerCommand.created_at.asc(), WorkerCommand.id.asc())
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
            "completed": int(counts.get("COMPLETED", 0)),
            "open": int(counts.get("PENDING", 0) + counts.get("RUNNING", 0)),
            "recent": [self._serialize_command(row) for row in rows],
        }

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
        runs = self.recent_runs(session, ["live_cycle", "bar_backfill"], limit=run_limit)
        commands = session.execute(
            select(WorkerCommand)
            .order_by(desc(WorkerCommand.created_at), desc(WorkerCommand.id))
            .limit(command_limit)
        ).scalars().all()
        events = self.recent_events(session, ["live_cycle", "bar_backfill"], limit=event_limit)
        return {
            "runs": [self._serialize_run_summary(run) for run in runs],
            "commands": [self._serialize_command(command) for command in commands],
            "events": [self._serialize_event(event) for event in events],
        }

    def runtime_snapshot(self, session: Session) -> dict[str, Any]:
        live_run = self.latest_run(session, "live_cycle")
        backfill_run = self.latest_run(session, "bar_backfill")
        events = list(reversed(self.recent_events(session, ["live_cycle", "bar_backfill"], limit=16)))
        worker = RuntimeControlService().get_worker_status(session)
        supervisor = RuntimeControlService().get_supervisor_status(session)
        queue = self.command_queue_snapshot(session)

        timestamps = [
            value
            for value in [
                live_run.updated_at if live_run else None,
                backfill_run.updated_at if backfill_run else None,
                events[-1].created_at if events else None,
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
            "recent_events": [self._serialize_event(event) for event in events],
        }

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
            "last_result": run.summary_json or None,
            "error": run.error_message,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "mode": run.trigger,
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
            "ts": event.created_at.isoformat() if event.created_at else None,
            "run_key": event.run_key,
        }

    def _serialize_command(self, command: WorkerCommand) -> dict[str, Any]:
        return {
            "id": command.id,
            "command_type": command.command_type,
            "status": command.status,
            "requested_by": command.requested_by,
            "created_at": command.created_at.isoformat() if command.created_at else None,
            "started_at": command.started_at.isoformat() if command.started_at else None,
            "finished_at": command.finished_at.isoformat() if command.finished_at else None,
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
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    def _parse_iso_dt(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
