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


def test_default_normalization_does_not_merge_same_theme_items(session, settings):
    now = datetime(2026, 1, 28, 14, 14, tzinfo=timezone.utc)
    session.add_all(
        [
            RawItem(
                source="reuters",
                source_tier=1,
                url="https://example.com/aapl-1",
                title="AAPL lowers outlook after weak iPhone demand",
                body="Apple lowered outlook after weak iPhone demand.",
                published_at=now,
                ingested_at=now,
                item_hash="hash-aapl-1",
                metadata_json={"ticker": "AAPL"},
                processed=False,
            ),
            RawItem(
                source="bloomberg",
                source_tier=1,
                url="https://example.com/aapl-2",
                title="AAPL cuts guidance as handset demand softens",
                body="Apple cuts guidance as handset demand softens.",
                published_at=now.replace(minute=20),
                ingested_at=now.replace(minute=20),
                item_hash="hash-aapl-2",
                metadata_json={"ticker": "AAPL"},
                processed=False,
            ),
        ]
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 2


def test_optional_merge_window_uses_last_evidence_timestamp(session, settings):
    merge_settings = settings.model_copy(update={"normalization_merge_window_min": 30})
    now = datetime(2026, 1, 28, 14, 14, tzinfo=timezone.utc)
    later = now.replace(minute=25)
    session.add_all(
        [
            RawItem(
                source="reuters",
                source_tier=1,
                url="https://example.com/msft-1",
                title="MSFT faces DOJ investigation",
                body="Microsoft faces a DOJ investigation.",
                published_at=now,
                ingested_at=now,
                item_hash="hash-msft-1",
                metadata_json={"ticker": "MSFT"},
                processed=False,
            ),
            RawItem(
                source="bloomberg",
                source_tier=1,
                url="https://example.com/msft-2",
                title="MSFT DOJ probe broadens",
                body="The DOJ probe into Microsoft broadens.",
                published_at=later,
                ingested_at=later,
                item_hash="hash-msft-2",
                metadata_json={"ticker": "MSFT"},
                processed=False,
            ),
        ]
    )
    session.flush()

    svc = NormalizationService(merge_settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_time == later


def test_unverified_metadata_ticker_is_dropped_for_mismatched_article(session, settings):
    now = datetime(2025, 10, 8, 12, 35, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="yahoo",
            source_tier=2,
            url="https://example.com/penguin-solutions",
            title="Penguin Solutions Earnings Beat Estimates. Why the Stock Has Dropped More Than 20%.",
            body="Penguin Solutions says its soft fiscal-year outlook is meant to reflect a broader set of outcomes.",
            published_at=now,
            ingested_at=now,
            item_hash="hash-penguin-meta-mismatch",
            metadata_json={"ticker": "META"},
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.tickers == []


def test_sec_earnings_release_uses_independent_event_type_and_summary_override(session, settings):
    now = datetime(2026, 1, 29, 21, 5, tzinfo=timezone.utc)
    session.add(
        RawItem(
            source="sec",
            source_tier=0,
            url="https://www.sec.gov/Archives/example/ex991.htm",
            title="AAPL Q1 revenue $124.3B and EPS $2.40; product revenue mixed but services set a record",
            body=(
                "LLM_SUMMARY_EN:\nApple 8-K earnings summary: quarterly revenue was $124.3 billion, EPS was $2.40, "
                "services revenue hit a record, but management noted softer demand in parts of the hardware portfolio.\n\nSEC_SOURCE_TEXT:\nItem 2.02 ..."
            ),
            published_at=now,
            ingested_at=now,
            item_hash="hash-sec-earnings-release-summary-override",
            metadata_json={
                "ticker": "AAPL",
                "form": "8-K",
                "event_type_hint": "sec_earnings_release",
                "summary_override": "Apple 8-K earnings summary: quarterly revenue was $124.3 billion, EPS was $2.40, services revenue hit a record, but management noted softer demand in parts of the hardware portfolio.",
            },
            processed=False,
        )
    )
    session.flush()

    svc = NormalizationService(settings)
    clusters = svc.build_clusters(session)
    assert len(clusters) == 1
    assert clusters[0].canonical.event_type == "sec_earnings_release"
    assert clusters[0].canonical.tickers == ["AAPL"]
    assert "$124.3 billion" in clusters[0].canonical.summary
