from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import close_all_positions_endpoint, list_live_entry_plan_events, live_positions, set_live_enabled
from app.broker.base import PositionInfo
from app.core.utils import utc_now
from app.db import database as db_database
from app.db.models import BacktestRun, WorkerCommand, WorkerRun
from app.ingestion.service import IngestionService
from app.ingestion.types import SourceCheck
from app.services.live_trading import LiveTradingService, _LIVE_CYCLE_MUTEX
from app.services.market_data import MarketDataService
from app.services.runtime_control import CONTROL_WORKER_HEARTBEAT, RuntimeControlService
from app.services.worker_runtime import (
    COMMAND_REFRESH_BARS,
    COMMAND_RUN_BACKTEST,
    COMMAND_RUN_INGESTION,
    COMMAND_RUN_LIVE_CYCLE,
    WorkerRuntimeService,
)
from app.worker import main as worker_main


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


def test_claim_next_command_prioritizes_live_commands_over_backtests(session) -> None:
    runtime = WorkerRuntimeService()
    runtime.queue_command(session, COMMAND_RUN_BACKTEST, payload={"trigger": "test"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_REFRESH_BARS, payload={"trigger": "test"}, requested_by="pytest")
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")

    claimed = runtime.claim_next_command(
        session,
        [COMMAND_RUN_BACKTEST, COMMAND_REFRESH_BARS, COMMAND_RUN_LIVE_CYCLE],
    )

    assert claimed is not None
    assert claimed.command_type == COMMAND_RUN_LIVE_CYCLE


def test_has_open_commands_detects_high_priority_backlog(session) -> None:
    runtime = WorkerRuntimeService()
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "test"}, requested_by="pytest")
    assert runtime.has_open_commands(session, [COMMAND_RUN_LIVE_CYCLE]) is True

    runtime.claim_next_command(session, [COMMAND_RUN_LIVE_CYCLE])
    assert runtime.has_open_commands(session, [COMMAND_RUN_LIVE_CYCLE]) is True


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


def test_list_live_entry_plan_events_filters_by_ticker_and_plan_id(session) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(session, run_type="live_cycle", trigger="manual", run_key="planapi01")
    runtime.add_event(
        session,
        "live_cycle",
        "AAPL waiting pullback",
        run=run,
        stage="entry_plan_evaluated",
        ticker="AAPL",
        payload={"plan_id": 11, "status": "waiting", "trigger_reason": "waiting pullback"},
    )
    runtime.add_event(
        session,
        "live_cycle",
        "MSFT triggered",
        run=run,
        stage="entry_plan_triggered",
        ticker="MSFT",
        payload={"plan_id": 12, "status": "triggered"},
    )
    session.commit()

    aapl_only = list_live_entry_plan_events(ticker="AAPL", plan_id=None, limit=20, session=session)
    assert aapl_only["count"] == 1
    assert aapl_only["items"][0]["ticker"] == "AAPL"
    assert aapl_only["items"][0]["plan_id"] == 11

    plan_only = list_live_entry_plan_events(ticker=None, plan_id=12, limit=20, session=session)
    assert plan_only["count"] == 1
    assert plan_only["items"][0]["ticker"] == "MSFT"
    assert plan_only["items"][0]["plan_id"] == 12


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
    assert result["live_enabled_at"] is not None
    assert result["live_warmup_until"] is not None
    commands = session.query(WorkerCommand).order_by(WorkerCommand.id.asc()).all()
    assert [command.command_type for command in commands] == [
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
    runtime.queue_command(session, COMMAND_RUN_INGESTION, payload={"trigger": "api"}, requested_by="pytest")

    result = set_live_enabled(body={"enabled": False}, session=session, settings=settings)

    assert result["enabled"] is False
    assert "Cancelled 3 pending live commands" in result["message"]

    commands = session.query(WorkerCommand).order_by(WorkerCommand.id.asc()).all()
    assert [command.status for command in commands] == ["CANCELLED", "CANCELLED", "CANCELLED", "PENDING"]

    queue = runtime.command_queue_snapshot(session)
    assert queue["pending"] == 1
    assert queue["cancelled"] == 3


def test_set_live_enabled_disable_cancel_orders_calls_broker(session, settings, monkeypatch) -> None:
    control = RuntimeControlService()
    runtime = WorkerRuntimeService()
    now = utc_now() - timedelta(minutes=1)
    control.touch_worker_heartbeat(session, pid=3101, started_at=now, extra={"configured_tickers": ["AAPL"]})
    control.touch_supervisor_heartbeat(session, pid=3102, started_at=now, extra={"child_pid": 3101})
    control.set_live_enabled(session, settings, True, source="pytest")
    runtime.queue_command(session, COMMAND_RUN_LIVE_CYCLE, payload={"trigger": "enable_live"}, requested_by="pytest")

    monkeypatch.setattr("app.broker.alpaca.AlpacaBroker.cancel_all_orders", lambda self: 2)

    result = set_live_enabled(
        body={"enabled": False, "disable_mode": "cancel_orders"},
        session=session,
        settings=settings,
    )

    assert result["enabled"] is False
    assert result["disable_mode"] == "CANCEL_ORDERS"
    assert result["broker_action"]["cancelled_orders"] == 2


def test_close_all_positions_endpoint_records_flatten(session, settings, monkeypatch) -> None:
    def _fake_flatten(self, session, **kwargs):
        return {
            "cycle_id": kwargs["cycle_id"],
            "requested_by": kwargs["requested_by"],
            "cancelled_orders": 3,
            "submitted_orders": 2,
            "errors": [],
            "before": {"gross_exposure": 12000.0},
            "after": {"gross_exposure": 0.0},
        }

    monkeypatch.setattr("app.api.routes.OvernightRiskService.flatten_all_positions", _fake_flatten)

    result = close_all_positions_endpoint(session=session, settings=settings)

    assert result["success"] is True
    assert result["requested_by"] == "manual_flatten"
    assert result["submitted_orders"] == 2


def test_live_positions_returns_exposure_summary(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.broker.alpaca.AlpacaBroker.get_account",
        lambda self: {"equity": "100000", "cash": "25000", "buying_power": "180000"},
    )
    monkeypatch.setattr(
        "app.broker.alpaca.AlpacaBroker.get_all_positions",
        lambda self: [
            PositionInfo(ticker="AAPL", quantity=10, avg_cost=100.0, market_value=1050.0, unrealized_pnl=50.0),
            PositionInfo(ticker="MSFT", quantity=-5, avg_cost=200.0, market_value=-980.0, unrealized_pnl=20.0),
        ],
    )
    monkeypatch.setattr(
        "app.broker.alpaca.AlpacaBroker.get_open_orders",
        lambda self: [{"id": "ord1"}, {"id": "ord2"}],
    )

    result = live_positions(settings=settings)

    assert result["gross_exposure"] == 2030.0
    assert result["net_exposure"] == 70.0
    assert result["unrealized_pnl_total"] == 70.0
    assert result["open_orders_count"] == 2
    assert result["risk_source_label"] == "已有持仓浮盈亏"


def test_fast_ingestion_profile_skips_sec(session, settings) -> None:
    svc = IngestionService(settings)

    def _sec_fetch(_session):
        raise AssertionError("SEC fetch should not run in fast profile")

    svc.sec.fetch = _sec_fetch  # type: ignore[method-assign]
    svc.rss.fetch = lambda: ([], [])  # type: ignore[method-assign]
    svc.rss.fetch_ticker_news = lambda tickers: ([], [])  # type: ignore[method-assign]
    svc.finnhub.fetch = lambda: ([], SourceCheck(source_key="finnhub", source_name="finnhub", source_type="news", display_name="Finnhub", status="ONLINE"))  # type: ignore[method-assign]
    svc.earnings_release.fetch_recent = lambda _session: ([], SourceCheck(source_key="earnings_release", source_name="earnings_release", source_type="earnings", display_name="Earnings Release", status="ONLINE"))  # type: ignore[method-assign]

    result = svc.run(session, profile="live_fast", tickers=["AAPL", "MSFT"])
    assert result.fetched == 0
    assert result.inserted == 0


def test_live_cycle_skips_when_another_cycle_is_running(session, settings) -> None:
    service = LiveTradingService(settings)
    acquired = _LIVE_CYCLE_MUTEX.acquire(blocking=False)
    assert acquired is True
    try:
        result = service.run_cycle(session, trigger="pytest")
    finally:
        _LIVE_CYCLE_MUTEX.release()

    assert result["skipped"] is True
    assert result["reason"] == "live_cycle_in_progress"


def test_stale_market_data_suppresses_order_before_price_fetch(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)
    service.settings.live_min_confidence = 0

    class DummyGraph:
        def run(self, _session, _ticker, context=None, progress_callback=None):
            return {
                "final_action": "BUY",
                "final_position_pct": 0.05,
                "final_reasoning": "positive catalyst",
                "portfolio_manager_result": {"confidence": 80, "metadata": {"action": "BUY", "position_pct": 0.05}},
            }

    class DummyBroker:
        def get_latest_price(self, _ticker):
            raise AssertionError("price fetch should not happen when cache is stale")

    monkeypatch.setattr(service, "_get_agent_graph", lambda: DummyGraph())
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 101,
            "event_type": "guidance_cut",
            "confidence": 85,
            "high_quality_source_count": 2,
        },
    )
    monkeypatch.setattr(
        service.market_data,
        "is_ticker_cache_fresh",
        lambda _session, _ticker, max_age_minutes=None: (False, 45.0),
    )

    result = service._process_ticker(
        session,
        DummyBroker(),
        "AAPL",
        portfolio_value=100_000.0,
        cycle_id="cycle123",
        msi={"et_time_str": "09:45 ET", "label": "open"},
        dry_run=False,
        run=None,
    )

    assert result["order_placed"] is False
    assert result["reason"] == "stale_market_data"
    assert result["cache_age_minutes"] == 45.0


def test_process_ticker_survives_progress_runtime_write_failure(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)
    runtime = WorkerRuntimeService()
    run = runtime.start_run(session, run_type="live_cycle", trigger="pytest", run_key="progress01")
    session.commit()

    class DummyGraph:
        def run(self, _session, _ticker, context=None, progress_callback=None):
            if progress_callback:
                progress_callback(
                    {
                        "stage": "parallel_agent_running",
                        "agent": "news_sentiment",
                        "message": "AAPL: news_sentiment analyzing",
                    }
                )
            return {
                "final_action": "BUY",
                "final_position_pct": 0.05,
                "final_reasoning": "progress write failure should not abort cycle",
                "portfolio_manager_result": {
                    "confidence": 81,
                    "metadata": {"action": "BUY", "position_pct": 0.05},
                },
            }

    service._agent_graph = DummyGraph()
    monkeypatch.setattr(
        service.runtime,
        "update_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runtime write failed")),
    )
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 301,
            "event_type": "earnings_release",
            "confidence": 90,
            "high_quality_source_count": 2,
        },
    )

    result = service._process_ticker(
        session=session,
        broker=object(),
        ticker="AAPL",
        portfolio_value=100_000.0,
        cycle_id="pytest-progress-failure",
        msi={"et_time_str": "18:05 ET", "label": "closed", "tradeable": False, "context_string": "closed"},
        dry_run=True,
        run=run,
        fast_path=False,
    )

    assert result["ticker"] == "AAPL"
    assert result["action"] == "BUY"
    assert result["dry_run"] is True
    assert session.execute(select(WorkerRun).where(WorkerRun.id == run.id)).scalar_one().id == run.id


def test_scheduled_live_cycle_uses_open_closed_intervals(session, monkeypatch) -> None:
    monkeypatch.setattr(worker_main, "market_session_info", lambda: {"label": "open"})
    monkeypatch.setattr(worker_main.settings, "live_open_cycle_seconds", 900, raising=False)
    open_interval, open_label = worker_main._target_live_interval_seconds()
    assert open_label == "open"
    assert open_interval == 900

    monkeypatch.setattr(worker_main, "market_session_info", lambda: {"label": "closed"})
    monkeypatch.setattr(worker_main.settings, "live_closed_cycle_seconds", 7200, raising=False)
    closed_interval, closed_label = worker_main._target_live_interval_seconds()
    assert closed_label == "closed"
    assert closed_interval == 7200


def test_scheduled_live_cycle_throttles_by_interval(session, monkeypatch) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(
        session,
        run_type="live_cycle",
        trigger="scheduled",
        run_key="throttle01",
        status="COMPLETED",
        stage="completed",
    )
    runtime.update_run(
        session,
        run,
        started_at=utc_now() - timedelta(minutes=30),
        updated_at=utc_now() - timedelta(minutes=29),
    )
    session.commit()

    monkeypatch.setattr(worker_main, "market_session_info", lambda: {"label": "closed"})
    monkeypatch.setattr(worker_main.settings, "live_closed_cycle_seconds", 7200, raising=False)

    should_run, meta = worker_main._should_run_scheduled_live_cycle(session)
    assert should_run is False
    assert meta["reason"] == "interval_not_elapsed"


def test_scheduled_live_cycle_reaps_stale_running_cycle(session, settings, monkeypatch) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(
        session,
        run_type="live_cycle",
        trigger="scheduled",
        run_key="stale999",
        status="RUNNING",
        stage="bar_refresh",
    )
    stale_at = utc_now() - timedelta(minutes=30)
    run.started_at = stale_at
    run.updated_at = stale_at
    session.flush()
    session.commit()

    monkeypatch.setattr(worker_main, "market_session_info", lambda: {"label": "open"})
    monkeypatch.setattr(worker_main.settings, "live_open_cycle_seconds", 900, raising=False)
    monkeypatch.setattr(worker_main.settings, "live_cycle_stale_seconds", 300, raising=False)

    should_run, meta = worker_main._should_run_scheduled_live_cycle(session)
    session.expire_all()
    reaped = session.get(WorkerRun, run.id)

    assert should_run is True
    assert meta["reason"] == "reaped_stale_previous_cycle"
    assert reaped is not None
    assert reaped.status == "FAILED"
    assert reaped.stage == "failed_stale"
    assert "stale live_cycle watchdog timed out" in (reaped.error_message or "")


def test_scheduled_live_cycle_does_not_reap_recent_postgres_naive_running_cycle(session, monkeypatch) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(
        session,
        run_type="live_cycle",
        trigger="scheduled",
        run_key="pgfresh01",
        status="RUNNING",
        stage="ingestion",
    )
    local_tz = timezone(timedelta(hours=-4))
    now_utc = datetime(2026, 4, 2, 14, 32, tzinfo=timezone.utc)
    fresh_local_naive = (now_utc - timedelta(seconds=120)).astimezone(local_tz).replace(tzinfo=None)
    run.started_at = fresh_local_naive
    run.updated_at = fresh_local_naive
    session.flush()
    session.commit()

    monkeypatch.setattr(db_database, "POSTGRES_NAIVE_LOCAL_TZ", local_tz, raising=False)
    monkeypatch.setattr(worker_main, "session_db_backend_name", lambda _session: "postgresql")
    monkeypatch.setattr(worker_main, "market_session_info", lambda: {"label": "open"})
    monkeypatch.setattr(worker_main, "utc_now", lambda: now_utc)
    monkeypatch.setattr(worker_main.settings, "live_open_cycle_seconds", 900, raising=False)
    monkeypatch.setattr(worker_main.settings, "live_cycle_stale_seconds", 300, raising=False)

    should_run, meta = worker_main._should_run_scheduled_live_cycle(session)
    session.expire_all()
    current = session.get(WorkerRun, run.id)

    assert should_run is False
    assert meta["reason"] == "previous_cycle_running"
    assert current is not None
    assert current.status == "RUNNING"


def test_refresh_bars_skips_when_another_refresh_is_running(session, settings) -> None:
    runtime = WorkerRuntimeService()
    active = runtime.start_run(
        session,
        run_type="bar_backfill",
        trigger="scheduled",
        run_key="bars1234",
        status="RUNNING",
        stage="fetching",
        total_tickers=1,
        completed_tickers=0,
    )
    session.commit()

    result = MarketDataService(settings).refresh_bars(
        session,
        start_date="2026-04-01",
        end_date="2026-04-02",
        tickers=["AAPL"],
        trigger="live_cycle",
    )

    running = session.query(WorkerRun).filter(WorkerRun.run_type == "bar_backfill", WorkerRun.status == "RUNNING").all()
    assert result["skipped"] is True
    assert result["reason"] == "refresh_already_running"
    assert result["active_run_key"] == active.run_key


def test_fail_stale_runs_ignores_recent_postgres_naive_run(session, monkeypatch) -> None:
    runtime = WorkerRuntimeService()
    run = runtime.start_run(
        session,
        run_type="bar_backfill",
        trigger="scheduled",
        run_key="pgbars01",
        status="RUNNING",
        stage="fetching",
    )
    local_tz = timezone(timedelta(hours=-4))
    now_utc = datetime(2026, 4, 2, 14, 32, tzinfo=timezone.utc)
    fresh_local_naive = (now_utc - timedelta(seconds=90)).astimezone(local_tz).replace(tzinfo=None)
    run.started_at = fresh_local_naive
    run.updated_at = fresh_local_naive
    session.flush()
    session.commit()

    monkeypatch.setattr(db_database, "POSTGRES_NAIVE_LOCAL_TZ", local_tz, raising=False)
    from app.services import worker_runtime as worker_runtime_module

    monkeypatch.setattr(worker_runtime_module, "session_db_backend_name", lambda _session: "postgresql")
    monkeypatch.setattr(worker_runtime_module, "utc_now", lambda: now_utc)

    stale = runtime.fail_stale_runs(
        session,
        run_type="bar_backfill",
        stale_after_seconds=300,
        reason="stale bar_backfill watchdog timed out",
    )
    session.expire_all()
    current = session.get(WorkerRun, run.id)

    assert stale == []
    assert current is not None
    assert current.status == "RUNNING"


def test_normalize_db_datetime_prefers_utc_candidate_for_new_postgres_rows(monkeypatch) -> None:
    local_tz = timezone(timedelta(hours=-4))
    now_utc = datetime(2026, 4, 2, 14, 32, tzinfo=timezone.utc)
    stored_utc_naive = datetime(2026, 4, 2, 14, 31)

    monkeypatch.setattr(db_database, "POSTGRES_NAIVE_LOCAL_TZ", local_tz, raising=False)
    monkeypatch.setattr(db_database, "utc_now", lambda: now_utc)

    normalized = db_database.normalize_db_datetime(stored_utc_naive, backend="postgresql")

    assert normalized == stored_utc_naive.replace(tzinfo=timezone.utc)
