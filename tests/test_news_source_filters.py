from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.db.models import Event, EventEvidence, RawItem
from app.tools.news import _metadata_ticker_pattern_clause, get_recent_events, get_ticker_news_summary


def _raw_item(
    *,
    source: str,
    title: str,
    url: str,
    item_hash: str,
    now: datetime,
) -> RawItem:
    return RawItem(
        source=source,
        source_tier=2,
        url=url,
        title=title,
        body=f"{title} body",
        published_at=now - timedelta(minutes=5),
        ingested_at=now - timedelta(minutes=4),
        item_hash=item_hash,
        metadata_json={"ticker": "AAPL"},
        processed=False,
    )


def _event(*, now: datetime, summary: str, ticker: str = "AAPL") -> Event:
    return Event(
        event_type="major_litigation",
        entities=[ticker],
        tickers=[ticker],
        severity=80,
        event_time=now - timedelta(minutes=5),
        confidence=90,
        validation_status="VALID",
        summary=summary,
    )


def test_ticker_news_summary_allowed_sources_normalizes_yahoo_finance(session):
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            _raw_item(
                source="Yahoo Finance",
                title="AAPL hits new catalyst",
                url="https://example.com/yahoo-aapl",
                item_hash="hash-yahoo-aapl",
                now=now,
            ),
            _raw_item(
                source="cnbc",
                title="AAPL market recap",
                url="https://example.com/cnbc-aapl",
                item_hash="hash-cnbc-aapl",
                now=now,
            ),
        ]
    )
    session.flush()

    items = get_ticker_news_summary(
        session,
        ticker="AAPL",
        lookback_hours=24,
        limit=10,
        allowed_sources=["yahoo"],
    )
    assert len(items) == 1
    assert items[0]["source"] == "yahoo_finance"


def test_metadata_ticker_pattern_clause_casts_json_for_postgres():
    compiled = str(
        sa.select(RawItem.id)
        .where(_metadata_ticker_pattern_clause("AAPL"))
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "CAST(raw_items.metadata_json AS TEXT)" in compiled


def test_get_recent_events_allowed_sources_filters_and_empty_fallback(session):
    now = datetime.now(timezone.utc)
    raw_sec = _raw_item(
        source="sec",
        title="AAPL sec event",
        url="https://example.com/sec-aapl",
        item_hash="hash-sec-event",
        now=now,
    )
    raw_benzinga = _raw_item(
        source="benzinga",
        title="AAPL benzinga event",
        url="https://example.com/benzinga-aapl",
        item_hash="hash-benzinga-event",
        now=now,
    )
    session.add_all([raw_sec, raw_benzinga])
    session.flush()

    event_sec = _event(now=now, summary="AAPL sec summary")
    event_benzinga = _event(now=now, summary="AAPL benzinga summary")
    session.add_all([event_sec, event_benzinga])
    session.flush()

    session.add_all(
        [
            EventEvidence(
                event_id=event_sec.id,
                raw_item_id=raw_sec.id,
                url=raw_sec.url,
                source="sec",
                source_tier=0,
                captured_at=now - timedelta(minutes=4),
                summary="sec evidence",
            ),
            EventEvidence(
                event_id=event_benzinga.id,
                raw_item_id=raw_benzinga.id,
                url=raw_benzinga.url,
                source="benzinga",
                source_tier=2,
                captured_at=now - timedelta(minutes=3),
                summary="benzinga evidence",
            ),
        ]
    )
    session.flush()

    sec_only = get_recent_events(
        session,
        ticker="AAPL",
        lookback_hours=24,
        limit=20,
        min_confidence=0,
        as_of=now,
        allowed_sources=["sec"],
    )
    sec_only_ids = {item["id"] for item in sec_only}
    assert sec_only_ids == {event_sec.id}

    no_filter = get_recent_events(
        session,
        ticker="AAPL",
        lookback_hours=24,
        limit=20,
        min_confidence=0,
        as_of=now,
        allowed_sources=[],
    )
    no_filter_ids = {item["id"] for item in no_filter}
    assert no_filter_ids == {event_sec.id, event_benzinga.id}
