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


def test_get_recent_events_honors_explicit_since(session):
    now = datetime.now(timezone.utc)
    old_event = _event(now=now - timedelta(hours=6), summary="AAPL old summary")
    old_event.event_time = now - timedelta(hours=6)
    old_event.created_at = now - timedelta(hours=6)
    new_event = _event(now=now, summary="AAPL new summary")
    new_event.event_time = now - timedelta(minutes=10)
    new_event.created_at = now - timedelta(minutes=9)
    session.add_all([old_event, new_event])
    session.flush()

    items = get_recent_events(
        session,
        ticker="AAPL",
        lookback_hours=48,
        since=now - timedelta(hours=1),
        limit=20,
        min_confidence=0,
        as_of=now,
    )

    assert {item["id"] for item in items} == {new_event.id}


def test_get_ticker_news_summary_honors_explicit_since(session):
    now = datetime.now(timezone.utc)
    old_item = _raw_item(
        source="benzinga",
        title="AAPL old catalyst",
        url="https://example.com/old-aapl",
        item_hash="hash-old-aapl",
        now=now - timedelta(hours=6),
    )
    old_item.published_at = now - timedelta(hours=6)
    old_item.ingested_at = now - timedelta(hours=6) + timedelta(minutes=1)
    new_item = _raw_item(
        source="benzinga",
        title="AAPL fresh catalyst",
        url="https://example.com/new-aapl",
        item_hash="hash-new-aapl",
        now=now,
    )
    session.add_all([old_item, new_item])
    session.flush()

    items = get_ticker_news_summary(
        session,
        ticker="AAPL",
        lookback_hours=48,
        since=now - timedelta(hours=1),
        limit=20,
    )

    assert [item["id"] for item in items] == [new_item.id]


def test_get_ticker_news_summary_rejects_short_ticker_metadata_false_positive(session):
    now = datetime.now(timezone.utc)
    noisy = RawItem(
        source="cnbc",
        source_tier=2,
        url="https://example.com/generic-banks",
        title="Consumers are anxious about the economy",
        body="Generic macro commentary about household spending and deposit trends.",
        published_at=now - timedelta(minutes=5),
        ingested_at=now - timedelta(minutes=4),
        item_hash="hash-generic-bac-noise",
        metadata_json={"ticker": "BAC"},
        processed=False,
    )
    real = RawItem(
        source="reuters",
        source_tier=1,
        url="https://example.com/bac-real",
        title="Bank of America expands wealth-management hiring",
        body="Bank of America said it is expanding wealth-management hiring across major regions.",
        published_at=now - timedelta(minutes=3),
        ingested_at=now - timedelta(minutes=2),
        item_hash="hash-bac-real",
        metadata_json={"ticker": "BAC", "structured_ticker": True, "matched_tickers": ["BAC"]},
        processed=False,
    )
    session.add_all([noisy, real])
    session.flush()

    items = get_ticker_news_summary(session, ticker="BAC", lookback_hours=24, limit=10)
    assert [item["id"] for item in items] == [real.id]


def test_get_ticker_news_summary_rejects_pg13_noise_for_pg(session):
    now = datetime.now(timezone.utc)
    noisy = RawItem(
        source="benzinga",
        source_tier=2,
        url="https://example.com/pg13",
        title="Streaming platform launches new PG-13 movie slate",
        body="Entertainment industry piece about movie ratings and release windows.",
        published_at=now - timedelta(minutes=5),
        ingested_at=now - timedelta(minutes=4),
        item_hash="hash-pg13-noise",
        metadata_json={"ticker": "PG"},
        processed=False,
    )
    real = RawItem(
        source="reuters",
        source_tier=1,
        url="https://example.com/pg-real",
        title="Procter & Gamble raises annual sales forecast",
        body="Procter & Gamble raised its annual sales forecast after strong demand in household categories.",
        published_at=now - timedelta(minutes=3),
        ingested_at=now - timedelta(minutes=2),
        item_hash="hash-pg-real",
        metadata_json={"ticker": "PG", "structured_ticker": True, "matched_tickers": ["PG"]},
        processed=False,
    )
    session.add_all([noisy, real])
    session.flush()

    items = get_ticker_news_summary(session, ticker="PG", lookback_hours=24, limit=10)
    assert [item["id"] for item in items] == [real.id]
