from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes import list_news
from app.db.models import RawItem


def _make_raw_item(*, source: str, title: str, url: str, item_hash: str, published_at: datetime, ingested_at: datetime) -> RawItem:
    return RawItem(
        source=source,
        source_tier=1,
        url=url,
        title=title,
        body=f"{title} body",
        published_at=published_at,
        ingested_at=ingested_at,
        item_hash=item_hash,
        metadata_json={"ticker": "AAPL"},
        processed=False,
    )


def test_list_news_defaults_to_published_time_order(session, settings):
    newer = datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc)
    older = datetime(2026, 1, 3, 9, 0, tzinfo=timezone.utc)

    # Insert newest first so it gets smaller id; second row has larger id but older publish time.
    first = _make_raw_item(
        source="rss",
        title="newer published",
        url="https://example.com/newer",
        item_hash="hash-newer",
        published_at=newer,
        ingested_at=newer + timedelta(minutes=2),
    )
    second = _make_raw_item(
        source="rss",
        title="older published",
        url="https://example.com/older",
        item_hash="hash-older",
        published_at=older,
        ingested_at=older + timedelta(minutes=2),
    )
    session.add_all([first, second])
    session.flush()

    payload = list_news(
        session=session,
        settings=settings,
        limit=20,
        since_id=None,
        before_id=None,
        source=None,
        q=None,
    )

    ids = [row["id"] for row in payload["items"]]
    assert ids == [first.id, second.id]


def test_list_news_marks_historical_backfill(session, settings):
    settings.news_backfill_delay_minutes = 180
    now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

    historical = _make_raw_item(
        source="sec",
        title="historical backfill item",
        url="https://example.com/historical",
        item_hash="hash-historical",
        published_at=now - timedelta(days=2),
        ingested_at=now,
    )
    fresh = _make_raw_item(
        source="rss",
        title="fresh item",
        url="https://example.com/fresh",
        item_hash="hash-fresh",
        published_at=now - timedelta(minutes=20),
        ingested_at=now,
    )
    session.add_all([historical, fresh])
    session.flush()

    payload = list_news(
        session=session,
        settings=settings,
        limit=20,
        since_id=None,
        before_id=None,
        source=None,
        q=None,
    )

    by_id = {row["id"]: row for row in payload["items"]}

    assert by_id[historical.id]["historical_backfill"] is True
    assert (by_id[historical.id]["backfill_delay_min"] or 0) >= 2880

    assert by_id[fresh.id]["historical_backfill"] is False
    assert (by_id[fresh.id]["backfill_delay_min"] or 0) < 180
