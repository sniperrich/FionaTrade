#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.ingestion.earnings_release_client import EarningsReleaseClient
from app.ingestion.service import IngestionService
from app.services.earnings_calendar import EarningsCalendarService


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill structured earnings release raw items into SQLite.")
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--symbols", default="", help="Comma-separated tickers, default all SP100")
    parser.add_argument(
        "--skip-calendar-refresh",
        action="store_true",
        help="Do not refresh Finnhub earnings calendar before materializing releases",
    )
    args = parser.parse_args()

    symbols = [token.strip().upper() for token in args.symbols.split(",") if token.strip()]

    init_db()
    settings = get_settings()
    with db_session() as session:
        refresh = None
        if not args.skip_calendar_refresh:
            refresh = EarningsCalendarService(settings).refresh(
                session,
                from_date=args.from_date,
                to_date=args.to_date,
                symbols=symbols or None,
            )

        items, check = EarningsReleaseClient(settings).build_from_calendar(
            session,
            from_date=args.from_date,
            to_date=args.to_date,
            symbols=symbols or None,
        )
        result = IngestionService(settings).persist_items(session, items, [check])

    refresh_text = (
        "calendar_refresh=skipped"
        if refresh is None
        else f"calendar_refresh fetched={refresh.fetched} upserted={refresh.upserted} skipped={refresh.skipped}"
    )
    print(
        f"{refresh_text} earnings_release fetched={result.fetched} inserted={result.inserted} "
        f"duplicates={result.duplicate_dropped} from={args.from_date} to={args.to_date}"
    )


if __name__ == "__main__":
    main()
