"""
Backfill Finnhub company-specific news into raw_items.

Usage examples:
  # Pull last 30 days for top 20 SP100 tickers
  python scripts/backfill_finnhub_news.py --from 2026-02-01 --to 2026-03-08

  # Pull specific tickers
  python scripts/backfill_finnhub_news.py --from 2026-01-01 --to 2026-03-08 --tickers AAPL,MSFT,NVDA

  # Dry run to see counts without writing
  python scripts/backfill_finnhub_news.py --from 2026-03-01 --to 2026-03-08 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import or_, select

from app.core.config import Settings
from app.core.utils import ensure_utc, normalize_title, utc_now
from app.db.database import db_session
from app.db.models import RawItem
from app.ingestion.finnhub_client import FinnhubNewsClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Top SP100 tickers most likely to generate tradeable events
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "JPM", "GS", "BAC", "MS", "WFC", "C",
    "LLY", "JNJ", "PFE", "ABBV", "MRK",
    "XOM", "CVX", "COP",
    "UNH", "COST", "WMT", "HD", "TGT",
    "NFLX", "DIS", "CMCSA",
    "AMD", "INTC", "QCOM", "TXN",
    "BA", "CAT", "HON", "GE", "RTX", "LMT", "GD",
    "V", "MA", "PYPL", "AXP",
    "BRK.B", "BLK",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Finnhub company news")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--tickers", default="", help="Comma-separated tickers (default: top 46 SP100)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but don't insert")
    parser.add_argument("--batch-size", type=int, default=10, help="Tickers per batch (for progress logging)")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else DEFAULT_TICKERS

    settings = Settings()
    if not settings.finnhub_api_key:
        logger.error("FINNHUB_API_KEY not set in .env")
        sys.exit(1)

    client = FinnhubNewsClient(settings)
    logger.info("Fetching company news: tickers=%d from=%s to=%s dry_run=%s",
                len(tickers), args.from_date, args.to_date, args.dry_run)

    # Process in batches for progress visibility
    total_fetched = total_inserted = total_skipped = 0

    for batch_start in range(0, len(tickers), args.batch_size):
        batch = tickers[batch_start: batch_start + args.batch_size]
        logger.info("Batch %d-%d / %d: %s", batch_start + 1, batch_start + len(batch), len(tickers), batch)

        items, check = client.fetch_company_news(batch, args.from_date, args.to_date)
        logger.info("  Fetched %d items (errors=%s)", len(items), check.details.get("errors", []))
        total_fetched += len(items)

        if args.dry_run or not items:
            continue

        with db_session() as session:
            # Load recent titles for dedup
            existing_titles = {
                normalize_title(r[0])
                for r in session.execute(select(RawItem.title)).all()
            }

            inserted = 0
            for item in items:
                nt = normalize_title(item.title)
                if nt in existing_titles:
                    total_skipped += 1
                    continue
                exists = session.execute(
                    select(RawItem.id)
                    .where(or_(RawItem.url == item.url, RawItem.item_hash == item.hash))
                    .limit(1)
                ).first()
                if exists:
                    total_skipped += 1
                    continue

                row = RawItem(
                    source=item.source,
                    source_tier=item.source_tier,
                    url=item.url,
                    title=item.title,
                    body=item.body,
                    published_at=ensure_utc(item.published_at),
                    ingested_at=ensure_utc(utc_now()),
                    item_hash=item.hash,
                    metadata_json={**item.metadata, "normalized_title": nt},
                    processed=False,
                )
                session.add(row)
                inserted += 1
                existing_titles.add(nt)

            logger.info("  Inserted %d / %d (skipped %d duplicates)",
                        inserted, len(items), len(items) - inserted)
            total_inserted += inserted

    logger.info("Done. fetched=%d inserted=%d skipped=%d dry_run=%s",
                total_fetched, total_inserted, total_skipped, args.dry_run)


if __name__ == "__main__":
    main()
