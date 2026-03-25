from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.api.routes import set_live_enabled
from app.core.utils import utc_now
from app.db.models import BacktestRun, WorkerCommand, WorkerRun
from app.services.runtime_control import CONTROL_WORKER_HEARTBEAT, RuntimeControlService
from app.services.worker_runtime import (
    COMMAND_REFRESH_BARS,
    COMMAND_RUN_INGESTION,
    COMMAND_RUN_LIVE_CYCLE,
    WorkerRuntimeService,
)


def test_worker_heartbeat_status_transitions(session) -> None:
    control = RuntimeControlService()
    started_at = utc_now() - timedelta(minutes=5)

    control.touch_worker_heartbeat(
        session,
        pid=4321,
        started_at=started_at,
        source="test_worker",
        scheduler_running=True,
        extra={"live_enabled": True, "configured_tickers": ["AAPL", "MSFT"]},
    )

    online = control.get_worker_status(session, stale_after_seconds=20)
    assert online["status"] == "ONLINE"
    assert online["online"] is True
    assert online["pid"] == 4321
    assert online["configured_tickers"] == ["AAPL", "MSFT"]

    control.set(
        session,
        CONTROL_WORKER_HEARTBEAT,
        {
            "pid": 4321,
            "source": "test_worker",
            "status": "ONLINE",
            "started_at": started_at.isoformat(),
            "last_seen_at": (utc_now() - timedelta(seconds=45)).isoformat(),
            "scheduler_running": True,
            "live_enabled": True,
            "configured_tickers": ["AAPL", "MSFT"],
        },
    )
    stale = control.get_worker_status(session, stale_after_seconds=20)
    assert stale["status"] == "STALE"
    assert stale["online"] is False

    control.mark_worker_offline(session, pid=4321, source="test_worker", reason="shutdown")
    offline = control.get_worker_status(session, stale_after_seconds=20)
    assert offline["status"] == "OFFLINE"
    assert offline["online"] is False
    assert offline["reason"] == "shutdown"


def test_worker_runtime_snapshot_includes_worker_and_queue(session) -> None:
    control = RuntimeControlService()
    runtime = WorkerRuntimeService()

    control.touch_worker_heartbeat(
        session,
        pid=99,
        started_at=utc_now() - timedelta(minutes=1),
        extra={"configured_tickers": ["NVDA"]},
    )
    control.touch_supervisor_heartbeat(
        session,
        pid=100,
        started_at=utc_now() - timedelta(minutes=2),
        extra={"child_pid": 99, "restart_count": 1},
    )

    pending = runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")
    runtime.claim_next_command(session, [COMMAND_RUN_LIVE_CYCLE])

    snapshot = runtime.runtime_snapshot(session)
    worker = snapshot["worker"]
    queue = snapshot["command_queue"]

    assert worker["status"] == "ONLINE"
    assert worker["pid"] == 99
    assert snapshot["supervisor"]["status"] == "ONLINE"
    assert snapshot["supervisor"]["pid"] == 100
    assert queue["pending"] == 1
    assert queue["running"] == 1
    assert queue["open"] == 2
    assert any(row["id"] == pending.id for row in queue["recent"])


def test_worker_runtime_snapshot_handles_naive_db_timestamps(session) -> None:
    control = RuntimeControlService()
    runtime = WorkerRuntimeService()

    control.touch_worker_heartbeat(
        session,
        pid=99,
        started_at=utc_now() - timedelta(minutes=1),
    )
    run = runtime.start_run(session, run_type="live_cycle", trigger="manual", run_key="naive1234")
    runtime.add_event(session, "live_cycle", "Cycle started", run=run, stage="starting", ticker="AAPL")
    session.commit()
    session.expire_all()

    snapshot = runtime.runtime_snapshot(session)
    assert snapshot["updated_at"]
    assert snapshot["worker"]["status"] == "ONLINE"
    assert snapshot["live_cycle"]["cycle_id"] == "naive1234"
    assert snapshot["live_cycle"]["updated_at"].endswith("+00:00")
    assert snapshot["recent_events"][0]["ts"].endswith("+00:00")


def test_worker_history_snapshot_contains_runs_commands_and_events(session) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(session, run_type="live_cycle", trigger="manual", run_key="abcd1234", total_tickers=2)
    runtime.add_event(session, "live_cycle", "Cycle started", run=run, stage="starting", ticker="AAPL")
    command = runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "manual"}, requested_by="pytest")
    runtime.complete_command(session, command, result={"ok": True})
    runtime.finish_run(session, run, summary={"cycle_id": "abcd1234"}, completed_tickers=2, total_tickers=2)

    history = runtime.history_snapshot(session, run_limit=5, command_limit=5, event_limit=5)
    assert history["runs"][0]["run_key"] == "abcd1234"
    assert history["commands"][0]["command_type"] == COMMAND_RUN_LIVE_CYCLE
    assert history["events"][0]["message"] == "Cycle started"


def test_worker_reconciles_orphaned_running_state(session) -> None:
    runtime = WorkerRuntimeService()
    command = WorkerCommand(
        command_type=COMMAND_RUN_LIVE_CYCLE,
        status="RUNNING",
        requested_by="pytest",
    )
    run = WorkerRun(
        run_key="orphan123",
        run_type="backtest",
        trigger="manual",
        status="RUNNING",
        stage="running",
    )
    backtest = BacktestRun(
        params={"start_date": "2026-01-01", "end_date": "2026-01-10"},
        metrics={"progress_total": 10, "progress_current": 3, "phase": "llm_prefetch"},
        status="RUNNING",
    )
    session.add_all([command, run, backtest])
    session.commit()

    counts = runtime.reconcile_orphaned_state(session)
    session.expire_all()

    reconciled_command = session.get(WorkerCommand, command.id)
    reconciled_run = session.get(WorkerRun, run.id)
    reconciled_backtest = session.get(BacktestRun, backtest.id)

    assert counts["commands_failed"] == 1
    assert counts["runs_failed"] == 1
    assert counts["backtests_failed"] == 1

    assert reconciled_command is not None
    assert reconciled_command.status == "FAILED"
    assert "worker restarted" in (reconciled_command.error_message or "")

    assert reconciled_run is not None
    assert reconciled_run.status == "FAILED"
    assert reconciled_run.stage == "failed"

    assert reconciled_backtest is not None
    assert reconciled_backtest.status == "FAILED"
    assert reconciled_backtest.metrics["phase"] == "failed"
    assert "worker restarted" in reconciled_backtest.metrics["error"]


def test_set_live_enabled_rejects_when_worker_or_supervisor_offline(session, settings) -> None:
    with pytest.raises(HTTPException) as excinfo:
        set_live_enabled(body={"enabled": True}, session=session, settings=settings)

    assert excinfo.value.status_code == 409
    assert "worker is offline or stale" in str(excinfo.value.detail)


def test_set_live_enabled_queues_commands_when_worker_and_supervisor_online(session, settings) -> None:
    control = RuntimeControlService()
    now = utc_now() - timedelta(minutes=1)
    control.touch_worker_heartbeat(session, pid=2001, started_at=now, extra={"configured_tickers": ["AAPL"]})
    control.touch_supervisor_heartbeat(session, pid=2002, started_at=now, extra={"child_pid": 2001})

    result = set_live_enabled(body={"enabled": True}, session=session, settings=settings)

    assert result["enabled"] is True
    commands = session.query(WorkerCommand).order_by(WorkerCommand.id.asc()).all()
    assert [command.command_type for command in commands] == [
        "run_ingestion_validation",
        "refresh_bars",
        "run_live_cycle",
    ]


def test_set_live_enabled_disables_and_cancels_pending_live_commands(session, settings) -> None:
    control = RuntimeControlService()
    runtime = WorkerRuntimeService()
    now = utc_now() - timedelta(minutes=1)
    control.touch_worker_heartbeat(session, pid=3001, started_at=now, extra={"configured_tickers": ["AAPL"]})
    control.touch_supervisor_heartbeat(session, pid=3002, started_at=now, extra={"child_pid": 3001})

    control.set_live_enabled(session, settings, True, source="pytest")
    runtime.queue_command(session, COMMAND_RUN_INGESTION, payload={"trigger": "enable_live"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_REFRESH_BARS, payload={"trigger": "enable_live"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "enable_live"}, requested_by="pytest")

    result = set_live_enabled(body={"enabled": False}, session=session, settings=settings)

    assert result["enabled"] is False
    assert "Cancelled 3 pending live commands" in result["message"]

    commands = session.query(WorkerCommand).order_by(WorkerCommand.id.asc()).all()
    assert [command.status for command in commands] == ["CANCELLED", "CANCELLED", "CANCELLED"]

    queue = runtime.command_queue_snapshot(session)
    assert queue["pending"] == 0
    assert queue["cancelled"] == 3
