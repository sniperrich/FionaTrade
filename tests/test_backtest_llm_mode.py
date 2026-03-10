from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event
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
        },
    )
    run = svc.get_run(session, result.run_id)
    assert run is not None
    assert result.metrics["trades"] == 1
    # max_position_pct=10%, suggestion=50% => 5% NAV => qty = 50 at $100 entry.
    assert abs(float(run.trade_log[0]["qty"]) - 50.0) < 1e-6
