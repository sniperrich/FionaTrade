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
                ts=event_time + timedelta(minutes=61),
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
                ts=event_time + timedelta(minutes=61),
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
            "flow_confirmation_enabled": False,
        },
    )

    assert result.status == "DONE"
    assert result.metrics["trades"] == 1
    row = svc.get_run(session, result.run_id)
    assert row is not None
    qty = float(row.trade_log[0]["qty"])
    assert 45.0 <= qty <= 55.0


def test_backtest_flow_soft_gate_scales_position(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=80,
            event_time=event_time,
            confidence=85,
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
                low=99.5,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=61),
                open=101.0,
                high=101.5,
                low=100.5,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.capital_confirmation.evaluate = lambda _session, **_kwargs: {  # noqa: SLF001
        "flow_score": 55,
        "flow_bucket": "MEDIUM",
        "position_multiplier": 0.80,
    }

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "hard_stops": False,
            "risk_sizing": False,
            "slippage_bps": 0.0,
            "flow_confirmation_enabled": True,
            "flow_confirmation_soft_gate": True,
            "horizon_min": 60,
        },
    )
    row = svc.get_run(session, result.run_id)
    assert row is not None
    assert result.metrics["trades"] == 1
    qty = float(row.trade_log[0]["qty"])
    # Base would be 150 shares (100000 * 0.15 / 100); soft gate 0.8 -> 120
    assert abs(qty - 120.0) < 1e-6
    assert row.trade_log[0]["flow_score"] == 55
    assert row.trade_log[0]["flow_bucket"] == "MEDIUM"
    assert abs(float(row.trade_log[0]["flow_position_multiplier"]) - 0.8) < 1e-9


def test_backtest_flow_weak_wait_breakout_can_expire(session, settings):
    event_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=80,
            event_time=event_time,
            confidence=85,
            validation_status="VALID",
            summary="buyback event",
        )
    )

    # Pre-event highs remain above all post-event closes, so no BUY breakout confirmation.
    session.add(
        Bar1m(
            ticker="AAPL",
            ts=event_time - timedelta(minutes=1),
            open=105.0,
            high=106.0,
            low=104.0,
            close=105.5,
            volume=1000.0,
            source="test",
        )
    )
    for i in range(1, 30):
        session.add(
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            )
        )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.capital_confirmation.evaluate = lambda _session, **_kwargs: {  # noqa: SLF001
        "flow_score": 30,
        "flow_bucket": "WEAK",
        "position_multiplier": 0.35,
    }

    result = svc.run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "min_confidence": 70,
            "hard_stops": False,
            "risk_sizing": False,
            "slippage_bps": 0.0,
            "flow_confirmation_enabled": True,
            "flow_confirmation_soft_gate": True,
            "flow_wait_valid_minutes": 20,
            "flow_breakout_lookback_min": 10,
            "horizon_min": 60,
        },
    )
    assert result.metrics["trades"] == 0
    assert result.metrics["flow_wait_mode_events"] >= 1
    assert result.metrics["flow_wait_expired"] >= 1
