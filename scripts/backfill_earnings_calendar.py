#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.services.earnings_calendar import EarningsCalendarService


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill SP100 earnings calendar into SQLite.")
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    init_db()
    settings = get_settings()
    with db_session() as session:
        result = EarningsCalendarService(settings).refresh(
            session,
            from_date=args.from_date,
            to_date=args.to_date,
        )

    print(
        f"earnings_calendar fetched={result.fetched} upserted={result.upserted} "
        f"skipped={result.skipped} from={result.from_date} to={result.to_date}"
    )


if __name__ == "__main__":
    main()
