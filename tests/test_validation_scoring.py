from __future__ import annotations

from datetime import timedelta

from app.db.models import RawItem
from app.normalization.service import NormalizationService
from app.validation.service import ValidationService
from app.core.utils import make_hash, utc_now


def test_validation_becomes_valid_on_two_sources(session, settings):
    now = utc_now()
    session.add(
        RawItem(
            source="reuters",
            source_tier=1,
            url="https://r/1",
            title="AAPL guidance cut after weak demand",
            body="Company lowered outlook",
            published_at=now,
            ingested_at=now,
            item_hash=make_hash("r", "1"),
            metadata_json={},
            processed=False,
        )
    )
    session.add(
        RawItem(
            source="bloomberg",
            source_tier=1,
            url="https://b/1",
            title="AAPL lowered outlook amid demand concerns",
            body="Guidance cut reiterated",
            published_at=now,
            ingested_at=now,
            item_hash=make_hash("b", "1"),
            metadata_json={},
            processed=False,
        )
    )
    session.flush()

    norm = NormalizationService(settings)
    clusters = norm.build_clusters(session)
    result = ValidationService().validate_and_store(session, clusters)

    assert result.created_events == 2
    assert result.valid_events == 1
    assert result.watch_events == 1


def test_validation_second_source_upgrades_without_merging(session, settings):
    now = utc_now()
    first = RawItem(
        source="reuters",
        source_tier=1,
        url="https://r/first",
        title="AAPL guidance cut after weak demand",
        body="Company lowered outlook on weak demand",
        published_at=now,
        ingested_at=now,
        item_hash=make_hash("r", "first"),
        metadata_json={},
        processed=False,
    )
    session.add(first)
    session.flush()

    norm = NormalizationService(settings)
    validator = ValidationService()
    first_clusters = norm.build_clusters(session)
    first_result = validator.validate_and_store(session, first_clusters)
    assert first_result.valid_events == 0
    assert first_result.watch_events == 1

    second = RawItem(
        source="bloomberg",
        source_tier=1,
        url="https://b/second",
        title="AAPL lowered outlook amid demand concerns",
        body="Guidance cut reiterated by management",
        published_at=now + timedelta(minutes=1),
        ingested_at=now + timedelta(minutes=1),
        item_hash=make_hash("b", "second"),
        metadata_json={},
        processed=False,
    )
    session.add(second)
    session.flush()

    second_clusters = norm.build_clusters(session)
    second_result = validator.validate_and_store(session, second_clusters)
    assert second_result.created_events == 1
    assert second_result.valid_events == 1


def test_validation_single_tier0_source_is_valid(session, settings):
    now = utc_now()
    session.add(
        RawItem(
            source="sec",
            source_tier=0,
            url="https://sec.example/aapl-8k",
            title="AAPL reports quarterly results under Item 2.02",
            body="Apple posted quarterly revenue of $143.8 billion and diluted EPS of $2.84.",
            published_at=now,
            ingested_at=now,
            item_hash=make_hash("sec", "tier0-aapl"),
            metadata_json={"ticker": "AAPL", "event_type_hint": "sec_earnings_release"},
            processed=False,
        )
    )
    session.flush()

    norm = NormalizationService(settings)
    clusters = norm.build_clusters(session)
    result = ValidationService().validate_and_store(session, clusters)

    assert result.created_events == 1
    assert result.valid_events == 1
    assert result.watch_events == 0
