from __future__ import annotations

from app.core.utils import utc_now
from app.db.models import RawItem
from app.monitoring.health import HealthAuditService


def test_health_snapshot_has_core_fields(session, settings):
    now = utc_now()
    session.add(
        RawItem(
            source="reuters",
            source_tier=1,
            url="https://example.com/a",
            title="sample",
            body="sample",
            published_at=now,
            ingested_at=now,
            item_hash="hash-1",
            metadata_json={},
            processed=False,
        )
    )
    session.flush()

    report = HealthAuditService(settings).snapshot(session, scheduler_running=True)

    assert report["status"] in {"ok", "warn"}
    assert report["db_ok"] is True
    assert "unprocessed_raw" in report
    assert "source_latency_sec" in report
    assert report["scheduler_running"] is True
