#!/usr/bin/env python
from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

from sqlalchemy import select

from app.core.utils import ensure_utc
from app.db.database import db_session
from app.db.models import Event

ROUTINE_FILING_RE = re.compile(
    r"\bfiled\s+(?:form\s+)?(?:8-k|10-k|10-q|6-k|13d|13g|sc\s*13d|sc\s*13g)\b",
    re.IGNORECASE,
)
MATERIAL_MARKERS = (
    "restatement",
    "material weakness",
    "internal control",
    "bankrupt",
    "chapter 11",
    "investigation",
    "sec charge",
    "doj",
    "fraud",
    "guidance cut",
    "lowered outlook",
    "earnings miss",
    "missed estimates",
    "major litigation",
    "class action",
    "accident",
    "explosion",
    "fire",
)


def _is_routine_filing_summary(summary: str) -> bool:
    text = (summary or "").lower().strip()
    if not text:
        return False
    if not ROUTINE_FILING_RE.search(text):
        return False
    if any(marker in text for marker in MATERIAL_MARKERS):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Relabel routine filing events to sec_filing.")
    parser.add_argument("--start-date", default="", help="Inclusive start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", default="", help="Exclusive end date (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true", help="Only show planned changes.")
    args = parser.parse_args()

    start_dt = ensure_utc(datetime.fromisoformat(args.start_date)) if args.start_date else None
    end_dt = ensure_utc(datetime.fromisoformat(args.end_date)) if args.end_date else None

    with db_session() as session:
        stmt = select(Event).order_by(Event.event_time.asc())
        if start_dt:
            stmt = stmt.where(Event.event_time >= start_dt)
        if end_dt:
            stmt = stmt.where(Event.event_time < end_dt)

        events = session.execute(stmt).scalars().all()

        by_old_type: Counter[str] = Counter()
        changed = 0
        for event in events:
            if event.event_type == "sec_filing":
                continue
            if not _is_routine_filing_summary(event.summary or ""):
                continue
            by_old_type[event.event_type or ""] += 1
            changed += 1
            if not args.dry_run:
                event.event_type = "sec_filing"
                event.severity = 55

        print(f"scanned={len(events)} changed={changed} dry_run={args.dry_run}")
        print("changes_by_old_type=", dict(by_old_type))


if __name__ == "__main__":
    main()
