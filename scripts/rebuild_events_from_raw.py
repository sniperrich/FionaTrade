#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.db.models import Event, EventEvidence, RawItem, Signal
from app.normalization.service import NormalizationService
from app.validation.service import ValidationService


def _parse_date(raw: str | None, end: bool = False) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if end:
        return dt
    return dt


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild events from raw_items using current normalization/validation logic.")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD (exclusive)")
    args = parser.parse_args()

    start_dt = _parse_date(args.start_date)
    end_dt = _parse_date(args.end_date)
    if start_dt is None or end_dt is None:
        raise SystemExit("invalid date range")

    settings = get_settings()
    init_db()

    with db_session() as session:
        raw_items = session.execute(
            select(RawItem)
            .where(RawItem.published_at >= start_dt, RawItem.published_at < end_dt)
            .order_by(RawItem.published_at.asc())
        ).scalars().all()
        raw_ids = [item.id for item in raw_items]
        if not raw_ids:
            print("no raw items found")
            return

        event_ids = session.execute(
            select(EventEvidence.event_id).where(EventEvidence.raw_item_id.in_(raw_ids)).distinct()
        ).scalars().all()
        if event_ids:
            session.execute(delete(Signal).where(Signal.event_id.in_(event_ids)))
            session.execute(delete(EventEvidence).where(EventEvidence.event_id.in_(event_ids)))
            session.execute(delete(Event).where(Event.id.in_(event_ids)))

        for item in raw_items:
            item.processed = False

        norm = NormalizationService(settings)
        validator = ValidationService(settings.validation_corroboration_window_minutes)
        clusters = norm.build_clusters(session, raw_ids=raw_ids)
        result = validator.validate_and_store(session, clusters)

    print(
        f"rebuild_events raw_items={len(raw_ids)} clusters={len(clusters)} "
        f"created={result.created_events} valid={result.valid_events} watch={result.watch_events}"
    )


if __name__ == "__main__":
    main()
