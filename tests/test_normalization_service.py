from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import RawItem
from app.normalization.service import NormalizationService


def test_routine_sec_filing_forced_to_sec_filing(session, settings):
    now = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="sec",
            source_tier=0,
            url="https://example.com/sec/aapl-8k",
            title="AAPL filed 8-K",
            body="SEC filing form 8-K accession 0000320193-26-000001.",
            published_at=now,
            ingested_at=now,
            item_hash="hash-routine-sec-filing",
            metadata_json={"ticker": "AAPL", "form": "8-K"},
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_type == "sec_filing"


def test_material_sec_filing_not_forced_to_sec_filing(session, settings):
    now = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="sec",
            source_tier=0,
            url="https://example.com/sec/aapl-material-8k",
            title="AAPL filed 8-K",
            body=(
                "The filing disclosed a material weakness in internal control "
                "and announced an accounting restatement."
            ),
            published_at=now,
            ingested_at=now,
            item_hash="hash-material-sec-filing",
            metadata_json={"ticker": "AAPL", "form": "8-K"},
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_type != "sec_filing"


def test_positive_earnings_headline_not_classified_as_earnings_miss(session, settings):
    now = datetime(2026, 1, 28, 14, 14, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="yahoo",
            source_tier=2,
            url="https://example.com/txn-guides-above",
            title="Texas Instruments Guides Above Q1 Estimates After Roughly In-Line Q4",
            body=(
                "Texas Instruments guided above first-quarter estimates after reporting "
                "roughly in-line fourth-quarter results and higher year-over-year revenue."
            ),
            published_at=now,
            ingested_at=now,
            item_hash="hash-positive-earnings-override",
            metadata_json={"ticker": "TXN"},
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_type == "unknown"


def test_positive_litigation_resolution_not_classified_as_negative(session, settings):
    now = datetime(2026, 1, 22, 16, 34, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="yahoo",
            source_tier=2,
            url="https://example.com/hon-litigation-settlement",
            title="Honeywell Settles Litigation With Flexjet. The Stock Rose.",
            body=(
                "Honeywell settled litigation with Flexjet, extended an engine maintenance deal, "
                "and the stock rose after the resolution."
            ),
            published_at=now,
            ingested_at=now,
            item_hash="hash-positive-litigation-override",
            metadata_json={"ticker": "HON"},
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_type == "unknown"
