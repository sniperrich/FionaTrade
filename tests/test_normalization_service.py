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
