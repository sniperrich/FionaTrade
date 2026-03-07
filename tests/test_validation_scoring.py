from __future__ import annotations

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

    assert result.created_events == 1
    assert result.valid_events == 1
    assert result.watch_events == 0
