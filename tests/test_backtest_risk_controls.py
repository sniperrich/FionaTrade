from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event


def test_backtest_hard_stop_exit_reason(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=70,
            event_time=event_time,
            confidence=80,
            validation_status="VALID",
            summary="buyback event",
        )
    )

    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            # Drops quickly to trigger stop-loss before horizon.
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=2),
                open=99.0,
                high=101.0,
                low=95.0,
                close=96.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=60),
                open=120.0,
                high=120.0,
                low=120.0,
                close=120.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "horizon_min": 60,
            "hard_stops": True,
            "risk_sizing": True,
            "risk_per_trade_pct": 0.005,
        },
    )

    assert result.status == "DONE"
    assert result.metrics["trades"] == 1
    assert result.metrics["exit_reason_counts"]["STOP"] == 1
    row = svc.get_run(session, result.run_id)
    assert row is not None
    assert row.trade_log[0]["exit_reason"] == "STOP"
    assert row.trade_log[0]["pnl"] < 0
    assert row.trade_log[0]["pnl"] > -400


def test_backtest_risk_position_sizing(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=70,
            event_time=event_time,
            confidence=80,
            validation_status="VALID",
            summary="buyback event",
        )
    )

    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=60),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "hard_stops": False,
            "risk_sizing": True,
            "risk_per_trade_pct": 0.001,
            "horizon_min": 60,
        },
    )

    assert result.status == "DONE"
    assert result.metrics["trades"] == 1
    row = svc.get_run(session, result.run_id)
    assert row is not None
    qty = float(row.trade_log[0]["qty"])
    assert 45.0 <= qty <= 55.0
