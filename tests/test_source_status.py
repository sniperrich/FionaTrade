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
