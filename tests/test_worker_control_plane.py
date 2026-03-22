from __future__ import annotations

from datetime import timedelta

from app.core.utils import utc_now
from app.services.runtime_control import CONTROL_WORKER_HEARTBEAT, RuntimeControlService
from app.services.worker_runtime import COMMAND_RUN_LIVE_CYCLE, WorkerRuntimeService


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

    pending = runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")
    runtime.claim_next_command(session, [COMMAND_RUN_LIVE_CYCLE])

    snapshot = runtime.runtime_snapshot(session)
    worker = snapshot["worker"]
    queue = snapshot["command_queue"]

    assert worker["status"] == "ONLINE"
    assert worker["pid"] == 99
    assert queue["pending"] == 1
    assert queue["running"] == 1
    assert queue["open"] == 2
    assert any(row["id"] == pending.id for row in queue["recent"])
