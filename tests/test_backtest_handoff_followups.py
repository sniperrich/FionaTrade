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
                ts=event_time + timedelta(minutes=61),
                open=101.0,
                high=101.0,
                low=100.5,
                close=101.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=6),
                open=102.0,
                high=103.0,
                low=101.5,
                close=102.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=66),
                open=103.0,
                high=103.5,
                low=102.5,
                close=103.0,
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
                ts=event_time + timedelta(minutes=61),
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


def test_backtest_exit_anchors_to_entry_time(session, settings):
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
                ts=event_time + timedelta(minutes=30),
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
                open=95.0,
                high=96.0,
                low=94.0,
                close=95.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=90),
                open=105.0,
                high=106.0,
                low=104.0,
                close=105.0,
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
            "start_date": "2026-01-09",
            "end_date": "2026-01-11",
            "min_confidence": 70,
            "horizon_min": 60,
            "hard_stops": False,
            "risk_sizing": False,
            "entry_window_min": 120,
            "use_signal_validation": False,
        },
    )
    run = svc.get_run(session, result.run_id)
    assert run is not None
    assert result.metrics["trades"] == 1
    assert run.trade_log[0]["exit_ts"].startswith((event_time + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S"))


def test_backtest_skips_routine_filing_headlines(session, settings):
    event_time = datetime(2026, 1, 10, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="regulatory_penalty",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=90,
            event_time=event_time,
            confidence=90,
            validation_status="VALID",
            summary="AAPL filed 8-K",
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=event_time + timedelta(minutes=120),
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
    result = svc.run(
        session,
        params={
            "start_date": "2026-01-09",
            "end_date": "2026-01-11",
            "min_confidence": 70,
            "horizon_min": 60,
            "hard_stops": False,
            "risk_sizing": False,
            "entry_window_min": 120,
            "use_signal_validation": False,
        },
    )
    assert result.metrics["trades"] == 0
    assert result.metrics["routine_filing_skipped"] == 1


def test_backtest_quality_filter_blocks_low_score(session, settings):
    event_time = datetime(2026, 1, 10, 14, 30, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="policy_shock",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=80,
            event_time=event_time,
            confidence=90,
            validation_status="VALID",
            summary="policy shock headline",
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
                ts=event_time + timedelta(minutes=120),
                open=99.0,
                high=100.0,
                low=98.0,
                close=99.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    svc.analysis.assess_event_quality = lambda _event, **_kwargs: {  # noqa: SLF001
        "quality": "LOW",
        "quality_score": 20,
        "reason": "noise",
        "model": "gemini-3-flash",
    }
    result = svc.run(
        session,
        params={
            "start_date": "2026-01-09",
            "end_date": "2026-01-11",
            "min_confidence": 70,
            "horizon_min": 60,
            "hard_stops": False,
            "risk_sizing": False,
            "entry_window_min": 120,
            "use_signal_validation": False,
            "use_event_quality_filter": True,
            "event_quality_min_score": 70,
        },
    )
    assert result.metrics["trades"] == 0
    assert result.metrics["quality_filtered"] == 1
