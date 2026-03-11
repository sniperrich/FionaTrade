from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.service import AnalysisService
from app.db.models import EarningsCalendar, Event, EventEvidence, RawItem


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

    ctx = svc._earnings_calendar_context(session, "AAPL", event_ts)
    assert ctx is not None
    assert ctx["last_report_date"] == "2025-10-01"
    assert ctx["days_since_last_report"] == 14
    assert ctx["next_report_date"] == "2025-10-28"
    assert ctx["days_to_next_report"] == 13
    assert ctx["last_surprise_pct"] == 25.0


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
