from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event
from app.core.utils import ensure_utc
from app.schemas.types import TradeSignal


def test_backtest_use_llm_mode(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="policy_shock",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=70,
        event_time=event_time,
        confidence=80,
        validation_status="VALID",
        summary="headline",
    )
    session.add(event)
    session.flush()

    session.add(
        Bar1m(
            ticker="AAPL",
            ts=event_time + timedelta(minutes=1),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000.0,
            source="test",
        )
    )
    session.add(
        Bar1m(
            ticker="AAPL",
            ts=event_time + timedelta(minutes=2),
            open=100.5,
            high=102.0,
            low=100.0,
            close=101.0,
            volume=1000.0,
            source="test",
        )
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.analysis.event_to_signal = lambda _event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="BUY",
        ticker="AAPL",
        confidence=80,
        horizon_min=1,
        reason="llm",
        expires_at=event_time + timedelta(minutes=1),
        fallback_used=False,
    )

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "min_confidence": 70,
            "use_llm": True,
            "use_signal_horizon": True,
            "use_signal_validation": False,  # disable validation: test exercises backtest mechanics only
        },
    )

    assert result.status == "DONE"
    assert result.metrics["use_llm"] is True
    assert result.metrics["llm_signals"] == 1
    assert result.metrics["llm_fallback_signals"] == 0
    assert result.metrics["trades"] == 1
    assert result.metrics["phase"] == "completed"
    assert result.metrics["phase_label"] == "Completed"
    assert result.metrics["last_progress_at"]


def test_backtest_llm_position_pct_suggestion_caps_position(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="policy_shock",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=70,
        event_time=event_time,
        confidence=80,
        validation_status="VALID",
        summary="headline",
    )
    session.add(event)
    session.flush()

    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=61),
                open=101.0,
                high=101.0,
                low=100.0,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.analysis.event_to_signal = lambda _event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="BUY",
        ticker="AAPL",
        confidence=80,
        horizon_min=60,
        position_pct_suggestion=0.5,
        reason="llm sized",
        expires_at=event_time + timedelta(minutes=60),
        fallback_used=False,
    )

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "min_confidence": 70,
            "use_llm": True,
            "use_signal_horizon": True,
            "hard_stops": False,
            "risk_sizing": False,
            "slippage_bps": 0.0,
            "use_signal_validation": False,
            "flow_confirmation_enabled": False,
        },
    )
    run = svc.get_run(session, result.run_id)
    assert run is not None
    assert result.metrics["trades"] == 1
    assert result.metrics["phase"] == "completed"
    expected_qty = settings.initial_nav * settings.max_position_pct * 0.5 / 100.0
    assert abs(float(run.trade_log[0]["qty"]) - expected_qty) < 1e-6


def test_event_backtest_honors_ticker_filter_and_custom_capital(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add_all(
        [
            Event(
                event_type="policy_shock",
                entities=["AAPL"],
                tickers=["AAPL"],
                severity=70,
                event_time=event_time,
                confidence=80,
                validation_status="VALID",
                summary="headline aapl",
            ),
            Event(
                event_type="policy_shock",
                entities=["MSFT"],
                tickers=["MSFT"],
                severity=70,
                event_time=event_time + timedelta(minutes=5),
                confidence=80,
                validation_status="VALID",
                summary="headline msft",
            ),
        ]
    )
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=2),
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="MSFT",
                ts=event_time + timedelta(minutes=6),
                open=200.0,
                high=201.0,
                low=199.0,
                close=200.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="MSFT",
                ts=event_time + timedelta(minutes=7),
                open=201.0,
                high=202.0,
                low=200.0,
                close=201.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.analysis.event_to_signal = lambda event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="BUY",
        ticker=event.tickers[0],
        confidence=80,
        horizon_min=1,
        reason="llm",
        expires_at=ensure_utc(event.event_time) + timedelta(minutes=1),
        fallback_used=False,
    )

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "tickers": ["AAPL"],
            "initial_capital": 50_000,
            "max_position_pct": 0.2,
            "min_confidence": 70,
            "use_llm": True,
            "use_signal_horizon": True,
            "use_signal_validation": False,
            "risk_sizing": False,
            "hard_stops": False,
            "slippage_bps": 0.0,
            "flow_confirmation_enabled": False,
        },
    )
    run = svc.get_run(session, result.run_id)

    assert run is not None
    assert result.status == "DONE"
    assert result.metrics["events_considered"] == 1
    assert result.metrics["initial_capital"] == 50_000
    assert result.metrics["max_position_pct"] == 0.2
    assert result.metrics["selected_tickers"] == ["AAPL"]
    assert len(run.trade_log) == 1
    assert run.trade_log[0]["ticker"] == "AAPL"
    assert abs(float(run.trade_log[0]["qty"]) - 100.0) < 1e-6


def test_event_backtest_intraday_flatten_exits_same_day_close(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="policy_shock",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=70,
        event_time=event_time,
        confidence=80,
        validation_status="VALID",
        summary="headline",
    )
    session.add(event)
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=datetime(2026, 1, 2, 20, 59, tzinfo=timezone.utc),
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.analysis.event_to_signal = lambda _event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="BUY",
        ticker="AAPL",
        confidence=80,
        horizon_min=600,
        reason="llm",
        expires_at=event_time + timedelta(minutes=600),
        fallback_used=False,
    )

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "min_confidence": 70,
            "use_llm": True,
            "use_signal_horizon": True,
            "intraday_flatten": True,
            "hard_stops": False,
            "risk_sizing": False,
            "slippage_bps": 0.0,
            "use_signal_validation": False,
            "flow_confirmation_enabled": False,
        },
    )
    run = svc.get_run(session, result.run_id)

    assert result.status == "DONE"
    assert result.metrics["intraday_flatten"] is True
    assert result.metrics["trades"] == 1
    assert run is not None
    assert run.trade_log[0]["exit_reason"] == "INTRADAY_FLAT"
    assert str(run.trade_log[0]["exit_ts"]).startswith("2026-01-02T20:59:00")
