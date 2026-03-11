from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event, EventEvidence, RawItem
from app.schemas.types import TradeSignal


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
        "allow_next_session_entry": False,
        "regular_session_only": False,
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
        "regular_session_only": False,
    }

    result_no_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": False})
    result_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": True})

    assert result_no_dedup.metrics["trades"] == 2
    assert result_dedup.metrics["trades"] == 1
    assert result_dedup.metrics["dedup_dropped"] == 1


def test_backtest_dedup_earnings_window_across_days(session, settings):
    event_time = datetime(2026, 1, 27, 21, 40, tzinfo=timezone.utc)
    session.add_all(
        [
            Event(
                event_type="earnings_miss",
                entities=["TXN"],
                tickers=["TXN"],
                severity=70,
                event_time=event_time,
                confidence=80,
                validation_status="VALID",
                summary="Texas Instruments misses estimates in fourth quarter",
            ),
            Event(
                event_type="earnings_miss",
                entities=["TXN"],
                tickers=["TXN"],
                severity=72,
                event_time=event_time + timedelta(hours=16),
                confidence=82,
                validation_status="VALID",
                summary="Texas Instruments posts another earnings update after quarterly results",
            ),
        ]
    )
    session.add_all(
        [
            Bar1m(
                ticker="TXN",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="TXN",
                ts=event_time + timedelta(minutes=61),
                open=99.0,
                high=100.0,
                low=98.0,
                close=99.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="TXN",
                ts=event_time + timedelta(hours=16, minutes=1),
                open=98.0,
                high=99.0,
                low=97.0,
                close=98.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="TXN",
                ts=event_time + timedelta(hours=17, minutes=1),
                open=97.0,
                high=98.0,
                low=96.0,
                close=97.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    base_params = {
        "start_date": "2026-01-27",
        "end_date": "2026-01-29",
        "min_confidence": 70,
        "horizon_min": 60,
        "hard_stops": False,
        "risk_sizing": False,
        "entry_window_min": 120,
        "use_signal_validation": False,
        "regular_session_only": False,
    }

    result_no_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": False})
    result_dedup = svc.run(session, params={**base_params, "dedup_same_day_event": True})

    assert result_no_dedup.metrics["trades"] == 2
    assert result_dedup.metrics["trades"] == 1
    assert result_dedup.metrics["earnings_window_dedup_dropped"] == 1


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
        "conviction_position_sizing": False,
        "regular_session_only": False,
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
                "regular_session_only": False,
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


def test_backtest_allows_unknown_events_in_llm_mode(session, settings):
    event_time = datetime(2026, 1, 22, 8, 0, tzinfo=timezone.utc)
    event = Event(
        event_type="unknown",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=55,
        event_time=event_time,
        confidence=80,
        validation_status="VALID",
        summary="company specific event headline",
    )
    session.add(event)
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=datetime(2026, 1, 22, 8, 1, tzinfo=timezone.utc),
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=datetime(2026, 1, 22, 10, 1, tzinfo=timezone.utc),
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
        horizon_min=120,
        reason="llm unknown allowed",
        expires_at=event_time + timedelta(minutes=120),
        fallback_used=False,
    )
    base_params = {
        "start_date": "2026-01-22",
        "end_date": "2026-01-23",
        "min_confidence": 30,
        "use_llm": True,
        "use_signal_validation": False,
        "hard_stops": False,
        "risk_sizing": False,
        "entry_window_min": 120,
        "allow_next_session_entry": False,
        "use_tradeability_filter": False,
        "regular_session_only": False,
    }
    blocked = svc.run(session, params={**base_params, "allow_unknown_with_llm": False})
    allowed = svc.run(session, params={**base_params, "allow_unknown_with_llm": True})
    assert blocked.metrics["trades"] == 0
    assert allowed.metrics["trades"] == 1


def test_backtest_allows_next_session_open_entry(session, settings):
    event_time = datetime(2026, 1, 22, 8, 0, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="policy_shock",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=70,
            event_time=event_time,
            confidence=80,
            validation_status="VALID",
            summary="policy event premarket",
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=datetime(2026, 1, 22, 14, 30, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=datetime(2026, 1, 22, 16, 30, tzinfo=timezone.utc),
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
        "start_date": "2026-01-22",
        "end_date": "2026-01-23",
        "min_confidence": 30,
        "use_llm": False,
        "use_signal_validation": False,
        "hard_stops": False,
        "risk_sizing": False,
        "horizon_min": 120,
        "entry_window_min": 120,
    }
    blocked = svc.run(session, params={**base_params, "allow_next_session_entry": False})
    allowed = svc.run(session, params={**base_params, "allow_next_session_entry": True})
    assert blocked.metrics["trades"] == 0
    assert blocked.metrics["entry_late_skipped"] == 1
    assert allowed.metrics["trades"] == 1
    assert allowed.metrics["next_session_entry_used"] == 1


def test_backtest_regular_session_only_uses_cash_open_not_premarket(session, settings):
    event_time = datetime(2026, 1, 12, 7, 0, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="policy_shock",
            entities=["JPM"],
            tickers=["JPM"],
            severity=75,
            event_time=event_time,
            confidence=85,
            validation_status="VALID",
            summary="premarket macro event",
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="JPM",
                ts=datetime(2026, 1, 12, 9, 0, tzinfo=timezone.utc),
                open=100.0,
                high=100.0,
                low=99.0,
                close=99.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="JPM",
                ts=datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc),
                open=99.0,
                high=100.0,
                low=98.0,
                close=99.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="JPM",
                ts=datetime(2026, 1, 12, 16, 30, tzinfo=timezone.utc),
                open=98.0,
                high=99.0,
                low=97.0,
                close=98.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    svc = BacktestEngineService(settings)
    params = {
        "start_date": "2026-01-12",
        "end_date": "2026-01-13",
        "min_confidence": 70,
        "horizon_min": 120,
        "hard_stops": False,
        "risk_sizing": False,
        "allow_next_session_entry": True,
        "entry_window_min": 120,
        "use_signal_validation": False,
    }
    premarket_allowed = svc.run(session, params={**params, "regular_session_only": False})
    cash_only = svc.run(session, params={**params, "regular_session_only": True})
    premarket_row = svc.get_run(session, premarket_allowed.run_id)
    cash_row = svc.get_run(session, cash_only.run_id)
    assert premarket_row is not None and cash_row is not None
    assert premarket_allowed.metrics["trades"] == 1
    assert cash_only.metrics["trades"] == 1
    assert premarket_row.trade_log[0]["entry_ts"].startswith("2026-01-12T09:00:00")
    assert cash_row.trade_log[0]["entry_ts"].startswith("2026-01-12T14:30:00")
    assert cash_only.metrics["next_session_entry_used"] == 1


def test_backtest_blocks_weekend_event_when_next_session_gap_too_large(session, settings):
    event_time = datetime(2026, 1, 24, 16, 6, tzinfo=timezone.utc)
    session.add(
        Event(
            event_type="major_litigation",
            entities=["HON"],
            tickers=["HON"],
            severity=80,
            event_time=event_time,
            confidence=85,
            validation_status="VALID",
            summary="weekend litigation update",
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="HON",
                ts=datetime(2026, 1, 26, 14, 30, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="HON",
                ts=datetime(2026, 1, 26, 16, 30, tzinfo=timezone.utc),
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
    params = {
        "start_date": "2026-01-24",
        "end_date": "2026-01-27",
        "min_confidence": 70,
        "horizon_min": 120,
        "hard_stops": False,
        "risk_sizing": False,
        "allow_next_session_entry": True,
        "entry_window_min": 120,
        "use_signal_validation": False,
        "regular_session_only": True,
    }
    blocked = svc.run(session, params={**params, "max_next_session_delay_min": 1080})
    allowed = svc.run(session, params={**params, "max_next_session_delay_min": 4000})
    assert blocked.metrics["trades"] == 0
    assert blocked.metrics["entry_late_skipped"] == 1
    assert allowed.metrics["trades"] == 1
    assert allowed.metrics["next_session_entry_used"] == 1


def test_backtest_tradeability_filter_blocks_opinion_content(session, settings):
    event_time = datetime(2026, 1, 22, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="unknown",
        entities=["Lockheed Martin"],
        tickers=["LMT"],
        severity=55,
        event_time=event_time,
        confidence=82,
        validation_status="VALID",
        summary="Lockheed Martin - Overbought After A Strong Run",
    )
    session.add(event)
    session.flush()

    raw = RawItem(
        source="seekingalpha",
        source_tier=2,
        url="https://example.com/lmt-opinion",
        title="Lockheed Martin - Overbought After A Strong Run",
        body="Lockheed Martin looks overbought after a strong run and valuation now appears stretched for investors.",
        published_at=event_time,
        item_hash="lmt-opinion",
    )
    session.add(raw)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source=raw.source,
            source_tier=raw.source_tier,
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="LMT",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="LMT",
                ts=event_time + timedelta(minutes=61),
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
    svc.analysis.event_to_signal = lambda _event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="SHORT",
        ticker="LMT",
        confidence=82,
        horizon_min=60,
        position_pct_suggestion=0.5,
        reason="should never trade",
        expires_at=event_time + timedelta(minutes=60),
        fallback_used=False,
    )
    result = svc.run(
        session,
        params={
            "start_date": "2026-01-22",
            "end_date": "2026-01-23",
            "min_confidence": 30,
            "use_llm": True,
            "use_signal_validation": False,
            "hard_stops": False,
            "risk_sizing": False,
            "use_tradeability_filter": True,
        },
    )
    assert result.metrics["trades"] == 0
    assert result.metrics["tradeability_filtered"] == 1
    assert result.metrics["tradeability_reason_counts"]["opinion_or_technical_commentary"] == 1


def test_backtest_conviction_position_sizing_lifts_strong_event_size(session, settings):
    event_time = datetime(2026, 1, 22, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="major_litigation",
        entities=["Honeywell"],
        tickers=["HON"],
        severity=82,
        event_time=event_time,
        confidence=88,
        validation_status="VALID",
        summary="Honeywell settles litigation with Flexjet and extends engine maintenance deal",
    )
    session.add(event)
    session.flush()

    raw = RawItem(
        source="marketwatch",
        source_tier=1,
        url="https://example.com/hon-litigation",
        title="Honeywell settles litigation with Flexjet and extends engine maintenance deal",
        body=(
            "Honeywell settled litigation with Flexjet, extended an engine maintenance agreement, "
            "and disclosed specific commercial terms in a company-focused update."
        ),
        published_at=event_time,
        item_hash="hon-litigation",
    )
    session.add(raw)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source=raw.source,
            source_tier=raw.source_tier,
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="HON",
                ts=event_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="HON",
                ts=event_time + timedelta(minutes=61),
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
    svc.analysis.event_to_signal = lambda _event, **_kwargs: TradeSignal(  # noqa: SLF001
        action="BUY",
        ticker="HON",
        confidence=88,
        horizon_min=60,
        position_pct_suggestion=0.2,
        reason="llm sized",
        expires_at=event_time + timedelta(minutes=60),
        fallback_used=False,
    )

    base_run = svc.run(
        session,
        params={
            "start_date": "2026-01-22",
            "end_date": "2026-01-23",
            "min_confidence": 30,
            "use_llm": True,
            "use_signal_validation": False,
            "hard_stops": False,
            "risk_sizing": True,
            "slippage_bps": 0.0,
            "use_tradeability_filter": True,
            "conviction_position_sizing": False,
        },
    )
    boosted_run = svc.run(
        session,
        params={
            "start_date": "2026-01-22",
            "end_date": "2026-01-23",
            "min_confidence": 30,
            "use_llm": True,
            "use_signal_validation": False,
            "hard_stops": False,
            "risk_sizing": True,
            "slippage_bps": 0.0,
            "use_tradeability_filter": True,
            "conviction_position_sizing": True,
        },
    )
    base_row = svc.get_run(session, base_run.run_id)
    boosted_row = svc.get_run(session, boosted_run.run_id)
    assert base_row is not None
    assert boosted_row is not None
    assert base_run.metrics["trades"] == 1
    assert boosted_run.metrics["trades"] == 1
    assert float(boosted_run.metrics["risk_per_trade_pct"]) == float(base_run.metrics["risk_per_trade_pct"])
    assert boosted_run.metrics["conviction_position_sizing"] is True
    assert float(boosted_row.trade_log[0]["qty"]) > float(base_row.trade_log[0]["qty"])
    assert float(boosted_row.trade_log[0]["effective_position_pct_suggestion"]) >= settings.backtest_conviction_position_floor
