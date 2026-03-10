from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event
from app.schemas.types import TradeSignal


def test_backtest_term_horizon_disabled_by_default(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="policy_shock",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=70,
        event_time=event_time,
        confidence=80,
        validation_status="VALID",
        summary="event",
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
            # Very bad early exit point.
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=2),
                open=90.0,
                high=91.0,
                low=89.0,
                close=90.0,
                volume=1000.0,
                source="test",
            ),
            # Better horizon exit.
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=60),
                open=110.0,
                high=111.0,
                low=109.0,
                close=110.0,
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
        horizon_min=1,
        horizon_profile="LONG",
        reason="term test",
        expires_at=event_time + timedelta(minutes=1),
        fallback_used=False,
    )

    result_disabled = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "horizon_min": 60,
            "use_llm": True,
            "use_signal_horizon": True,
            "enable_term_horizon": False,
            "hard_stops": False,
            "risk_sizing": False,
            "use_signal_validation": False,  # disable: test exercises horizon mechanics only
        },
    )

    result_enabled = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "horizon_min": 60,
            "use_llm": True,
            "use_signal_horizon": True,
            "enable_term_horizon": True,
            "hard_stops": False,
            "risk_sizing": False,
            "use_signal_validation": False,  # disable: test exercises horizon mechanics only
        },
    )

    assert result_disabled.metrics["trades"] == 1
    assert result_enabled.metrics["trades"] == 1
    assert result_disabled.metrics["total_return"] > result_enabled.metrics["total_return"]
