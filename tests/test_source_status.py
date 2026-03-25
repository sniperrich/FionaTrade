from __future__ import annotations

from app.core.utils import make_hash, utc_now
from app.db.models import SourceStatus
from app.ingestion.service import IngestionService
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem


def test_ingestion_persists_source_status(session, settings):
    svc = IngestionService(settings)
    now = utc_now()
    item = RawNewsItem(
        source="reuters",
        url="https://x/a",
        title="Apple cuts forecast",
        body="AAPL guidance cut",
        published_at=now,
        ingested_at=now,
        hash=make_hash("reuters", "https://x/a", "Apple cuts forecast"),
        source_tier=1,
    )

    checks = [
        SourceCheck(
            source_key="sec",
            source_name="sec",
            source_type="sec",
            display_name="SEC EDGAR",
            status="OFFLINE",
            error_message="timeout",
        ),
        SourceCheck(
            source_key="rss:reuters",
            source_name="reuters",
            source_type="rss",
            display_name="RSS REUTERS",
            status="ONLINE",
        ),
    ]

    svc._collect = lambda _session: ([item], checks)  # noqa: SLF001
    svc.run(session)

    rows = session.query(SourceStatus).order_by(SourceStatus.source_key.asc()).all()
    assert len(rows) == 2
    assert rows[0].source_key == "rss:reuters"
    assert rows[0].status == "ONLINE"
    assert rows[0].error_message is None
    assert rows[1].source_key == "sec"
    assert rows[1].status == "OFFLINE"
    assert rows[1].error_message == "timeout"


def test_ingestion_merges_duplicate_source_checks_by_source_key(session, settings):
    svc = IngestionService(settings)

    checks = [
        SourceCheck(
            source_key="rss:feeds.bbci.co.uk",
            source_name="bbc",
            source_type="rss",
            display_name="RSS BBC (feeds.bbci.co.uk)",
            status="ONLINE",
            details={"feed": "https://feeds.bbci.co.uk/news/world/rss.xml", "items": 12},
        ),
        SourceCheck(
            source_key="rss:feeds.bbci.co.uk",
            source_name="bbc",
            source_type="rss",
            display_name="RSS BBC (feeds.bbci.co.uk)",
            status="OFFLINE",
            error_message="xml parse error",
            details={"feed": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"},
        ),
    ]

    svc._persist_source_checks(session, checks)  # noqa: SLF001
    session.flush()

    rows = session.query(SourceStatus).filter(SourceStatus.source_key == "rss:feeds.bbci.co.uk").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ONLINE"
    assert row.error_message is None
    assert row.details_json["online_count"] == 1
    assert row.details_json["offline_count"] == 1
    assert len(row.details_json["checks"]) == 2
