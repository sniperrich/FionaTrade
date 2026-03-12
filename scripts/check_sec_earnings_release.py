#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.ingestion.sec_client import SecClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Check SEC 8-K Item 2.02 earnings release extraction for one ticker.")
    parser.add_argument("--ticker", required=True, help="Ticker, e.g. AAPL")
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    init_db()
    settings = get_settings()
    client = SecClient(settings)

    with db_session() as session:
        client._round_robin_tickers = lambda _session, _universe, batch_size=10: [ticker]  # noqa: SLF001
        items, check = client.fetch(session)

    sec_earnings = [item for item in items if (item.metadata or {}).get("event_type_hint") == "sec_earnings_release"]
    payload = {
        "ticker": ticker,
        "source_check": {
            "status": check.status,
            "error_message": check.error_message,
            "details": check.details,
        },
        "sec_earnings_release_count": len(sec_earnings),
        "latest": None
        if not sec_earnings
        else {
            "title": sec_earnings[0].title,
            "published_at": sec_earnings[0].published_at.isoformat(),
            "url": sec_earnings[0].url,
            "metadata": sec_earnings[0].metadata,
            "body_preview": sec_earnings[0].body[:1200],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
