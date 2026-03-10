from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event


def test_backtest_entry_window_configurable(session, settings):
    event_time = datetime(2026, 1, 10, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=80,
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
                ts=event_time + timedelta(minutes=90),
                open=100.0,
                high=101.0,
                low=99.0,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=150),
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    base_params = {
        "start_date": "2026-01-09",
        "end_date": "2026-01-11",
        "min_confidence": 70,
        "horizon_min": 60,
        "hard_stops": False,
        "risk_sizing": False,
        "use_signal_validation": False,
    }

    result_60 = svc.run(session, params={**base_params, "entry_window_min": 60})
    result_120 = svc.run(session, params={**base_params, "entry_window_min": 120})

    assert result_60.metrics["trades"] == 0
    assert result_120.metrics["trades"] == 1


def test_backtest_dedup_same_day_event(session, settings):
    event_time = datetime(2026, 1, 10, 14, 30, tzinfo=timezone.utc)
    session.add_all(
        [
            Event(
                event_type="buyback",
                entities=["AAPL"],
                tickers=["AAPL"],
                severity=65,
                event_time=event_time,
                confidence=80,
                validation_status="VALID",
                summary="buyback mention 1",
            ),
            Event(
                event_type="buyback",
                entities=["AAPL"],
                tickers=["AAPL"],
                severity=90,
                event_time=event_time + timedelta(minutes=5),
                confidence=85,
                validation_status="VALID",
                summary="buyback mention 2",
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
                close=100.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=120),
                open=101.0,
                high=101.0,
                low=100.5,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    base_params = {
        "start_date": "2026-01-09",
        "end_date": "2026-01-11",
        "min_confidence": 70,
        "horizon_min": 60,
        "hard_stops": False,
        "risk_sizing": False,
        "entry_window_min": 120,
        "use_signal_validation": False,
    }

    result_no_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": False})
    result_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": True})

    assert result_no_dedup.metrics["trades"] == 2
    assert result_dedup.metrics["trades"] == 1
    assert result_dedup.metrics["dedup_dropped"] == 1


def test_backtest_regime_risk_adjust_multiplier(session, settings):
    event_time = datetime(2026, 1, 10, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="buyback",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=85,
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
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=60),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="SPY",
                ts=event_time - timedelta(days=28),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="SPY",
                ts=event_time,
                open=110.0,
                high=110.0,
                low=110.0,
                close=110.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    base_params = {
        "start_date": "2026-01-09",
        "end_date": "2026-01-11",
        "min_confidence": 70,
        "horizon_min": 60,
        "hard_stops": False,
        "risk_sizing": True,
        "risk_per_trade_pct": 0.001,
        "entry_window_min": 120,
        "use_signal_validation": False,
    }

    baseline = svc.run(session, params={**base_params, "regime_risk_adjust": False})
    bull_adj = svc.run(
        session,
        params={
            **base_params,
            "regime_risk_adjust": True,
            "regime_bull_risk_multiplier": 1.2,
            "regime_bear_risk_multiplier": 0.8,
        },
    )

    base_run = svc.get_run(session, baseline.run_id)
    bull_run = svc.get_run(session, bull_adj.run_id)
    assert base_run is not None and bull_run is not None

    qty_base = float(base_run.trade_log[0]["qty"])
    qty_bull = float(bull_run.trade_log[0]["qty"])
    assert qty_bull > qty_base
    assert bull_run.trade_log[0]["regime"] == "BULL"
