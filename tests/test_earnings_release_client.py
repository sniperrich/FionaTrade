from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import EarningsCalendar, SourceStatus
from app.ingestion.earnings_release_client import EarningsReleaseClient
from app.ingestion.service import IngestionService
from app.normalization.service import NormalizationService


def test_earnings_release_client_builds_structured_item(session, settings):
    session.add(
        EarningsCalendar(
            symbol="AAPL",
            report_date=datetime(2025, 10, 30, 0, 0, tzinfo=timezone.utc),
            report_hour="amc",
            quarter=4,
            fiscal_year=2025,
            eps_actual=2.35,
            eps_estimate=2.10,
            revenue_actual=100_000_000_000,
            revenue_estimate=99_000_000_000,
            source="finnhub",
        )
    )
    session.flush()

    items, check = EarningsReleaseClient(settings).build_from_calendar(
        session,
        from_date="2025-10-30",
        to_date="2025-10-30",
    )

    assert check.status == "ONLINE"
    assert check.error_message is None
    assert len(items) == 1
    item = items[0]
    assert item.source == "earnings_release"
    assert item.source_tier == 0
    assert item.metadata["ticker"] == "AAPL"
    assert item.metadata["structured_ticker"] is True
    assert item.metadata["event_type_hint"] == "unknown"
    assert item.published_at.date().isoformat() == "2025-10-30"
    assert "reports quarterly results" in item.title
    assert "EPS actual" in item.body


def test_earnings_release_ingestion_persists_source_status_and_ticker(session, settings):
    session.add(
        EarningsCalendar(
            symbol="TXN",
            report_date=datetime(2025, 10, 22, 0, 0, tzinfo=timezone.utc),
            report_hour="bmo",
            quarter=3,
            fiscal_year=2025,
            eps_actual=1.05,
            eps_estimate=1.20,
            revenue_actual=3_900_000_000,
            revenue_estimate=4_100_000_000,
            source="finnhub",
        )
    )
    session.flush()

    client = EarningsReleaseClient(settings)
    items, check = client.build_from_calendar(
        session,
        from_date="2025-10-22",
        to_date="2025-10-22",
    )
    persisted = IngestionService(settings).persist_items(session, items, [check])
    clusters = NormalizationService(settings).build_clusters(session, raw_ids=persisted.raw_item_ids)

    assert persisted.inserted == 1
    assert len(clusters) == 1
    assert clusters[0].canonical.tickers == ["TXN"]
    assert clusters[0].canonical.event_type == "earnings_miss"

    status = session.query(SourceStatus).filter(SourceStatus.source_key == "earnings_release").one()
    assert status.status == "ONLINE"
