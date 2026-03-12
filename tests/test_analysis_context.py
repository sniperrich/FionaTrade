from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.service import AnalysisService
from app.db.models import Bar1m, EarningsCalendar, Event, EventEvidence, RawItem


def test_earnings_calendar_context_uses_last_and_next_reports(session, settings):
    svc = AnalysisService(settings)
    event_ts = datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc)
    session.add_all(
        [
            EarningsCalendar(
                symbol="AAPL",
                report_date=datetime(2025, 10, 1, tzinfo=timezone.utc),
                report_hour="amc",
                quarter=3,
                fiscal_year=2025,
                eps_actual=1.25,
                eps_estimate=1.00,
            ),
            EarningsCalendar(
                symbol="AAPL",
                report_date=datetime(2025, 10, 28, tzinfo=timezone.utc),
                report_hour="amc",
                quarter=4,
                fiscal_year=2025,
            ),
        ]
    )
    session.flush()

    ctx = svc._earnings_calendar_context(session, "AAPL", event_ts, include_upcoming=True)
    assert ctx is not None
    assert ctx["last_report_date"] == "2025-10-01"
    assert ctx["days_since_last_report"] == 14
    assert ctx["next_report_date"] == "2025-10-28"
    assert ctx["days_to_next_report"] == 13
    assert ctx["last_surprise_pct"] == 25.0


def test_earnings_calendar_context_historical_excludes_future_schedule(session, settings):
    svc = AnalysisService(settings)
    event_ts = datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc)
    session.add_all(
        [
            EarningsCalendar(
                symbol="AAPL",
                report_date=datetime(2025, 10, 1, tzinfo=timezone.utc),
                report_hour="amc",
                quarter=3,
                fiscal_year=2025,
                eps_actual=1.25,
                eps_estimate=1.00,
            ),
            EarningsCalendar(
                symbol="AAPL",
                report_date=datetime(2025, 10, 28, tzinfo=timezone.utc),
                report_hour="amc",
                quarter=4,
                fiscal_year=2025,
            ),
        ]
    )
    session.flush()

    ctx = svc._earnings_calendar_context(session, "AAPL", event_ts)
    assert ctx is not None
    assert ctx["last_report_date"] == "2025-10-01"
    assert "next_report_date" not in ctx
    assert "days_to_next_report" not in ctx


def test_evidence_rows_filter_future_evidence(session, settings):
    svc = AnalysisService(settings)
    event_ts = datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc)
    event = Event(
        event_type="major_litigation",
        tickers=["AAPL"],
        entities=["AAPL"],
        severity=70,
        confidence=70,
        validation_status="VALID",
        summary="AAPL faces court ruling",
        event_time=event_ts,
    )
    session.add(event)
    session.flush()

    raw_now = RawItem(
        source="reuters",
        source_tier=1,
        url="https://example.com/now",
        title="AAPL faces court ruling",
        body="Apple faces a court ruling today.",
        published_at=event_ts,
        ingested_at=event_ts,
        item_hash="analysis-context-now",
        metadata_json={},
        processed=True,
    )
    raw_future = RawItem(
        source="bloomberg",
        source_tier=1,
        url="https://example.com/future",
        title="AAPL favorable ruling sparks outlook debate",
        body="This article was published after the event timestamp and must be ignored.",
        published_at=event_ts + timedelta(minutes=15),
        ingested_at=event_ts + timedelta(minutes=15),
        item_hash="analysis-context-future",
        metadata_json={},
        processed=True,
    )
    session.add_all([raw_now, raw_future])
    session.flush()
    session.add_all(
        [
            EventEvidence(
                event_id=event.id,
                raw_item_id=raw_now.id,
                url=raw_now.url,
                source=raw_now.source,
                source_tier=raw_now.source_tier,
                summary=raw_now.title,
            ),
            EventEvidence(
                event_id=event.id,
                raw_item_id=raw_future.id,
                url=raw_future.url,
                source=raw_future.source,
                source_tier=raw_future.source_tier,
                summary=raw_future.title,
            ),
        ]
    )
    session.flush()

    rows = svc._evidence_rows(session, event)
    assert len(rows) == 1
    assert rows[0]["url"] == raw_now.url


def test_build_earnings_review_flags_high_bar_ticker(session, settings):
    svc = AnalysisService(settings)
    asof = datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc)
    report_dates = [
        datetime(2025, 7, 31, tzinfo=timezone.utc),
        datetime(2025, 8, 28, tzinfo=timezone.utc),
        datetime(2025, 9, 25, tzinfo=timezone.utc),
    ]
    for idx, report_date in enumerate(report_dates):
        session.add(
            EarningsCalendar(
                symbol="AAPL",
                report_date=report_date,
                report_hour="amc",
                quarter=idx + 1,
                fiscal_year=2025,
                eps_actual=1.20,
                eps_estimate=1.00,
            )
        )
        trade_day = report_date + timedelta(days=1)
        open_ts = trade_day.replace(hour=13, minute=30)
        exit_ts = trade_day.replace(hour=15, minute=30)
        session.add_all(
            [
                Bar1m(
                    ticker="AAPL",
                    ts=open_ts,
                    open=100.0,
                    high=100.5,
                    low=99.5,
                    close=100.0,
                    volume=1000,
                    source="test",
                ),
                Bar1m(
                    ticker="AAPL",
                    ts=exit_ts,
                    open=98.0,
                    high=98.5,
                    low=97.0,
                    close=97.5,
                    volume=1000,
                    source="test",
                ),
            ]
        )
    session.flush()

    review = svc.build_earnings_review(session, "AAPL", asof)
    assert review is not None
    assert review["sample_size"] == 3
    assert review["beat_and_drop_rate"] == 1.0
    assert review["tradeability"] == "POOR"
    assert review["high_bar_score"] >= 70

    event = Event(
        event_type="earnings_miss",
        tickers=["AAPL"],
        entities=["AAPL"],
        severity=70,
        confidence=70,
        validation_status="VALID",
        summary="AAPL quarterly earnings update and management commentary",
        event_time=asof,
    )
    tradeability = svc.assess_tradeability(event, session=session)
    assert tradeability["tradeable"] is False
    assert tradeability["reason"] == "earnings_high_bar_risk"
