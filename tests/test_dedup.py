from __future__ import annotations

from datetime import timedelta

from app.core.utils import make_hash, utc_now
from app.ingestion.service import IngestionService
from app.schemas.types import RawNewsItem


def test_ingestion_dedup_title_and_hash(session, settings):
    svc = IngestionService(settings)
    now = utc_now()
    item1 = RawNewsItem(
        source="reuters",
        url="https://x/a",
        title="Apple cuts forecast",
        body="AAPL guidance cut",
        published_at=now,
        ingested_at=now,
        hash=make_hash("reuters", "https://x/a", "Apple cuts forecast"),
        source_tier=1,
    )
    item2 = RawNewsItem(
        source="reuters",
        url="https://x/a2",
        title="Apple cuts forecast",  # same normalized title in window
        body="Same news rewrite",
        published_at=now + timedelta(minutes=1),
        ingested_at=now,
        hash=make_hash("reuters", "https://x/a2", "Apple cuts forecast"),
        source_tier=1,
    )

    svc._collect = lambda _session: ([item1, item2], [])  # noqa: SLF001
    result = svc.run(session)

    assert result.inserted == 1
    assert result.duplicate_dropped == 1
