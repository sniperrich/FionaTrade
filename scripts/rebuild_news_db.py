"""
rebuild_news_db.py — 一键清空旧新闻并回填历史数据

用法:
  # 第一步: 预检所有数据源（不写库）
  python scripts/rebuild_news_db.py --dry-run

  # 第二步: 清空新闻表并回填 Dec 2025 ~ Feb 2026
  python scripts/rebuild_news_db.py --from 2025-12-01 --to 2026-02-28

  # 只回填特定ticker（快速测试）
  python scripts/rebuild_news_db.py --from 2025-12-01 --to 2026-02-28 --tickers AAPL,NVDA

  # 跳过清空直接追加（已清空过时用）
  python scripts/rebuild_news_db.py --from 2025-12-01 --to 2026-02-28 --no-clear

说明:
  - 只清空 raw_items / events / event_evidence / source_status
  - 保留 Bar1m / MacroIndicator / FundamentalsSnapshot / AnalystRating / EarningsCalendar
  - 数据来源: Finnhub company-news (主力, 每ticker每日限100条)
  - 归一化: 清空后重新 normalize + validate 生成 Event 记录
  - event_time = published_at (新闻发布时间, 非入库时间) —— 无未来函数
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text

from app.core.config import Settings
from app.core.utils import ensure_utc, normalize_title, utc_now
from app.db.database import db_session
from app.db.models import Event, EventEvidence, RawItem, SourceStatus
from app.ingestion.finnhub_client import FinnhubNewsClient
from app.normalization.service import NormalizationService
from app.validation.service import ValidationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Ticker list ──────────────────────────────────────────────────────────────
# Sorted roughly by news volume / trading relevance
DEFAULT_TICKERS = [
    # Mega-cap tech (highest Finnhub coverage)
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO",
    # Semiconductors
    "AMD", "INTC", "QCOM", "TXN", "AMAT", "MU",
    # Financials
    "JPM", "GS", "BAC", "MS", "WFC", "C", "BLK", "AXP", "V", "MA", "PYPL", "SCHW",
    # Healthcare
    "LLY", "JNJ", "PFE", "ABBV", "MRK", "UNH", "TMO", "DHR",
    # Energy
    "XOM", "CVX", "COP",
    # Consumer
    "COST", "WMT", "HD", "TGT", "MCD", "NKE", "SBUX", "LOW",
    # Media / Comm
    "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
    # Cloud / Enterprise SW
    "CRM", "NOW", "ORCL", "IBM", "ADBE", "INTU",
    # Industrial / Defense
    "BA", "CAT", "HON", "GE", "RTX", "LMT", "GD", "EMR", "UNP",
    # Other
    "BRK.B", "PM", "MO", "KO", "PEP", "PG", "ABT", "MDT", "GILD", "AMGN",
]


# ── Source tester ─────────────────────────────────────────────────────────────

def test_sources(settings: Settings) -> None:
    """Print a live audit of every data source — no DB writes."""
    import feedparser

    RSS_FEEDS = [
        ("CNBC General",  "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("CNBC Markets",  "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
        ("CNBC Economy",  "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("CNBC Tech",     "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
        ("MarketWatch",   "https://www.marketwatch.com/rss/topstories"),
        ("NYT Business",  "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("FT Markets",    "https://www.ft.com/markets?format=rss"),
        ("Yahoo Finance", "https://finance.yahoo.com/rss/topstories"),
    ]
    SAMPLE_TICKER_FEEDS = ["AAPL", "NVDA", "AMZN"]

    print("\n" + "=" * 70)
    print("DATA SOURCE AUDIT (live check)")
    print("=" * 70)

    print("\n[1] Broad RSS feeds:")
    rss_total = 0
    for name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            n = len(feed.entries)
            rss_total += n
            has_body = sum(1 for e in feed.entries if len(e.get("summary", "") or "") > 50)
            status = "✅" if n > 0 else "❌"
            latest_pub = feed.entries[0].get("published", "?")[:22] if n else "—"
            sample = (feed.entries[0].get("title") or "")[:65] if n else "—"
            print(f"  {status} {name:<22} {n:3d} arts ({has_body} w/body) | {latest_pub}")
            print(f"       Sample: {sample}")
        except Exception as e:
            print(f"  ❌ {name:<22} ERROR: {e}")
    print(f"  → RSS total: {rss_total} articles this cycle")

    print("\n[2] Per-ticker Yahoo Finance RSS (sample 3 tickers):")
    for ticker in SAMPLE_TICKER_FEEDS:
        url = f"https://finance.yahoo.com/rss/headline?s={ticker}"
        try:
            feed = feedparser.parse(url)
            n = len(feed.entries)
            pub = feed.entries[0].get("published", "")[:22] if n else "—"
            sample = (feed.entries[0].get("title") or "")[:65] if n else "—"
            print(f"  {'✅' if n else '❌'} {ticker:<6} {n:3d} arts | {pub}")
            print(f"       Sample: {sample}")
        except Exception as e:
            print(f"  ❌ {ticker}: {e}")

    print("\n[3] Finnhub company-news (last 7 days for AAPL):")
    if not settings.finnhub_api_key:
        print("  ❌ FINNHUB_API_KEY not set")
    else:
        import httpx
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = datetime.now(timezone.utc)
        from datetime import timedelta
        week_ago_str = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        r = httpx.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": "AAPL", "from": week_ago_str, "to": today,
                    "token": settings.finnhub_api_key},
            timeout=15,
        )
        if r.status_code == 200:
            items = r.json()
            print(f"  ✅ AAPL last 7d: {len(items)} articles")
            if items:
                print(f"     Latest: {items[0].get('headline', '')[:70]}")
                print(f"     Body sample: {items[0].get('summary', '')[:120]}")
        else:
            print(f"  ❌ HTTP {r.status_code}")

    print("\n" + "=" * 70)
    print("Dry-run complete. Run without --dry-run to start rebuild.")
    print("=" * 70 + "\n")


# ── Clear news tables ─────────────────────────────────────────────────────────

def clear_news_tables() -> None:
    """Delete raw_items, events, event_evidence, source_status (news only).
    Preserves: Bar1m, MacroIndicator, FundamentalsSnapshot, AnalystRating,
               EarningsCalendar, BacktestRun, BacktestTrade, Position, etc.
    """
    with db_session() as session:
        # Count before
        n_raw   = session.execute(select(func.count()).select_from(RawItem)).scalar()
        n_ev    = session.execute(select(func.count()).select_from(Event)).scalar()
        n_evev  = session.execute(select(func.count()).select_from(EventEvidence)).scalar()

        logger.info("Before clear: raw_items=%d  events=%d  event_evidence=%d",
                    n_raw, n_ev, n_evev)

        # Must delete in FK order: EventEvidence → Event → RawItem
        evev_del = session.execute(delete(EventEvidence))
        ev_del   = session.execute(delete(Event))
        raw_del  = session.execute(delete(RawItem))
        ss_del   = session.execute(delete(SourceStatus))

        logger.info("Deleted: event_evidence=%d  events=%d  raw_items=%d  source_status=%d",
                    evev_del.rowcount, ev_del.rowcount, raw_del.rowcount, ss_del.rowcount)

    logger.info("✅ News tables cleared")


# ── Finnhub backfill ──────────────────────────────────────────────────────────

def backfill_finnhub(
    settings: Settings,
    tickers: list[str],
    from_date: str,
    to_date: str,
    batch_size: int = 5,
) -> list[int]:
    """Fetch Finnhub company-news for the given tickers and date range.
    Returns list of inserted raw_item IDs (for normalization).
    """
    client = FinnhubNewsClient(settings)
    all_inserted_ids: list[int] = []

    logger.info("Backfilling Finnhub company-news: %d tickers  %s → %s",
                len(tickers), from_date, to_date)

    for batch_start in range(0, len(tickers), batch_size):
        batch = tickers[batch_start: batch_start + batch_size]
        batch_label = f"[{batch_start+1}-{batch_start+len(batch)}/{len(tickers)}]"
        logger.info("%s Fetching: %s", batch_label, ", ".join(batch))

        items, check = client.fetch_company_news(batch, from_date, to_date)
        errors = check.details.get("errors", []) if check.details else []
        logger.info("%s  Got %d items (errors=%s)", batch_label, len(items), errors or "none")

        if not items:
            continue

        with db_session() as session:
            # Load all existing titles + hashes for dedup
            existing_hashes: set[str] = {
                r[0] for r in session.execute(select(RawItem.item_hash)).all() if r[0]
            }
            existing_urls: set[str] = {
                r[0] for r in session.execute(select(RawItem.url)).all() if r[0]
            }

            inserted = 0
            skipped = 0
            for item in items:
                if item.hash in existing_hashes or item.url in existing_urls:
                    skipped += 1
                    continue

                nt = normalize_title(item.title)
                row = RawItem(
                    source=item.source,
                    source_tier=item.source_tier,
                    url=item.url,
                    title=item.title,
                    body=item.body,
                    published_at=ensure_utc(item.published_at),
                    ingested_at=utc_now(),
                    item_hash=item.hash,
                    metadata_json={**item.metadata, "normalized_title": nt},
                    processed=False,
                )
                session.add(row)
                session.flush()
                all_inserted_ids.append(row.id)
                existing_hashes.add(item.hash)
                existing_urls.add(item.url)
                inserted += 1

            logger.info("%s  Inserted %d, skipped %d duplicates", batch_label, inserted, skipped)

        # Finnhub rate limit: 150 calls/min → ~5 tickers/batch at 0.4s each = safe
        time.sleep(0.5)

    logger.info("Finnhub backfill complete: %d total raw_items inserted", len(all_inserted_ids))
    return all_inserted_ids


# ── Normalize + validate ──────────────────────────────────────────────────────

def normalize_all(settings: Settings, raw_ids: list[int] | None = None) -> None:
    """Run NormalizationService + ValidationService to generate Event rows.

    If raw_ids is provided, only process those items.
    Otherwise processes all unprocessed raw_items.
    """
    norm_svc = NormalizationService(settings)
    val_svc  = ValidationService()

    with db_session() as session:
        # Count unprocessed
        unprocessed_count = session.execute(
            select(func.count()).select_from(RawItem).where(RawItem.processed == False)  # noqa: E712
        ).scalar()
        logger.info("Normalizing: %d unprocessed raw_items (raw_ids filter=%s)",
                    unprocessed_count, len(raw_ids) if raw_ids else "none (all)")

        clusters = norm_svc.build_clusters(session, raw_ids=raw_ids)
        logger.info("Built %d clusters from normalization", len(clusters))

        if not clusters:
            logger.info("No clusters to validate — done")
            return

        result = val_svc.validate_and_store(session, clusters)
        logger.info("Validation complete: created_events=%d  valid_events=%d",
                    result.created_events, result.valid_events)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> None:
    with db_session() as session:
        n_raw  = session.execute(select(func.count()).select_from(RawItem)).scalar()
        n_ev   = session.execute(select(func.count()).select_from(Event)).scalar()
        n_evev = session.execute(select(func.count()).select_from(EventEvidence)).scalar()

        # Per-source breakdown
        source_rows = session.execute(
            select(RawItem.source, func.count().label("n"))
            .group_by(RawItem.source)
            .order_by(text("n DESC"))
        ).all()

        # Per-event-type breakdown
        type_rows = session.execute(
            select(Event.event_type, func.count().label("n"))
            .group_by(Event.event_type)
            .order_by(text("n DESC"))
        ).all()

        # Date range
        date_range = session.execute(
            select(func.min(RawItem.published_at), func.max(RawItem.published_at))
        ).first()

        print("\n" + "=" * 70)
        print("DATABASE SUMMARY AFTER REBUILD")
        print("=" * 70)
        print(f"  raw_items:      {n_raw:,}")
        print(f"  events:         {n_ev:,}")
        print(f"  event_evidence: {n_evev:,}")
        if date_range and date_range[0]:
            print(f"  date range:     {str(date_range[0])[:19]} → {str(date_range[1])[:19]}")

        print("\n  [raw_items by source]")
        for source, count in source_rows:
            print(f"    {source:<25} {count:5d}")

        print("\n  [events by type]")
        for etype, count in type_rows:
            print(f"    {etype:<35} {count:5d}")

        print("=" * 70 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild news DB: clear old data → backfill history → normalize"
    )
    parser.add_argument(
        "--from", dest="from_date", default="2025-12-01",
        help="Backfill start date YYYY-MM-DD (default: 2025-12-01)"
    )
    parser.add_argument(
        "--to", dest="to_date", default="2026-02-28",
        help="Backfill end date YYYY-MM-DD (default: 2026-02-28)"
    )
    parser.add_argument(
        "--tickers", default="",
        help="Comma-separated tickers. Default: top ~60 SP100 tickers"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only test data sources, no DB changes"
    )
    parser.add_argument(
        "--no-clear", action="store_true",
        help="Skip clearing tables (append mode)"
    )
    parser.add_argument(
        "--skip-normalize", action="store_true",
        help="Skip normalization step (insert raw_items only)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=5,
        help="Tickers per Finnhub API batch (default: 5)"
    )
    args = parser.parse_args()

    settings = Settings()

    # Step 0: always run source test
    test_sources(settings)

    if args.dry_run:
        logger.info("--dry-run mode: exiting after source test")
        return

    if not settings.finnhub_api_key:
        logger.error("FINNHUB_API_KEY not set in .env — cannot backfill")
        sys.exit(1)

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers else DEFAULT_TICKERS
    )

    logger.info("=" * 60)
    logger.info("REBUILD PLAN")
    logger.info("  Date range : %s → %s", args.from_date, args.to_date)
    logger.info("  Tickers    : %d", len(tickers))
    logger.info("  Clear DB   : %s", not args.no_clear)
    logger.info("  Normalize  : %s", not args.skip_normalize)
    logger.info("=" * 60)

    # Step 1: clear
    if not args.no_clear:
        logger.info("Step 1/3: Clearing news tables...")
        clear_news_tables()
    else:
        logger.info("Step 1/3: Skipped (--no-clear)")

    # Step 2: backfill Finnhub history
    logger.info("Step 2/3: Backfilling Finnhub company-news...")
    inserted_ids = backfill_finnhub(
        settings, tickers, args.from_date, args.to_date, batch_size=args.batch_size
    )

    # Step 3: normalize
    if not args.skip_normalize:
        logger.info("Step 3/3: Normalizing → Events...")
        normalize_all(settings, raw_ids=inserted_ids if inserted_ids else None)
    else:
        logger.info("Step 3/3: Skipped (--skip-normalize)")

    # Final summary
    print_summary()
    logger.info("✅ rebuild_news_db complete")


if __name__ == "__main__":
    main()
