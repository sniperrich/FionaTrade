from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest_engine.service import BacktestEngineService
from app.db.models import Bar1m, Event, EventEvidence, RawItem


def test_backtest_source_filter_limits_events_and_trades(session, settings):
    base_time = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)

    yahoo_raw = RawItem(
        source="yahoo",
        source_tier=1,
        url="https://example.com/yahoo-aapl",
        title="AAPL buyback headline",
        body="AAPL announces a large buyback.",
        published_at=base_time,
        item_hash="hash-yahoo-aapl",
    )
    sec_raw = RawItem(
        source="sec",
        source_tier=0,
        url="https://example.com/sec-msft",
        title="MSFT filed a material item",
        body="MSFT release",
        published_at=base_time + timedelta(minutes=10),
        item_hash="hash-sec-msft",
    )
    session.add_all([yahoo_raw, sec_raw])
    session.flush()

    yahoo_event = Event(
        event_type="buyback",
        entities=["Apple"],
        tickers=["AAPL"],
        severity=80,
        event_time=base_time,
        confidence=85,
        validation_status="VALID",
        summary="Apple announces buyback",
    )
    sec_event = Event(
        event_type="buyback",
        entities=["Microsoft"],
        tickers=["MSFT"],
        severity=80,
        event_time=base_time + timedelta(minutes=10),
        confidence=85,
        validation_status="VALID",
        summary="Microsoft announces buyback",
    )
    session.add_all([yahoo_event, sec_event])
    session.flush()

    session.add_all(
        [
            EventEvidence(
                event_id=yahoo_event.id,
                raw_item_id=yahoo_raw.id,
                url=yahoo_raw.url,
                source="yahoo",
                source_tier=1,
                summary="Yahoo evidence",
            ),
            EventEvidence(
                event_id=sec_event.id,
                raw_item_id=sec_raw.id,
                url=sec_raw.url,
                source="sec",
                source_tier=0,
                summary="SEC evidence",
            ),
        ]
    )

    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=base_time + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=base_time + timedelta(minutes=121),
                open=102.0,
                high=102.0,
                low=101.0,
                close=102.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="MSFT",
                ts=base_time + timedelta(minutes=11),
                open=200.0,
                high=201.0,
                low=199.0,
                close=200.5,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="MSFT",
                ts=base_time + timedelta(minutes=131),
                open=202.0,
                high=202.0,
                low=201.0,
                close=202.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.flush()

    result = BacktestEngineService(settings).run(
        session,
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-10",
            "sources": ["yahoo"],
            "hard_stops": False,
            "risk_sizing": False,
            "slippage_bps": 0.0,
            "use_signal_validation": False,
            "use_tradeability_filter": False,
        },
    )

    assert result.status == "DONE"
    assert result.metrics["events_considered"] == 1
    assert result.metrics["trades"] == 1
    assert result.metrics["selected_sources"] == ["yahoo_finance"]
    assert set(result.metrics["source_attribution"].keys()) == {"yahoo_finance"}
