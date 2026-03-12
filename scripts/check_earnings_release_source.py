#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis.service import AnalysisService
from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.db.models import Event
from app.ingestion.earnings_release_client import EarningsReleaseClient
from app.ingestion.service import IngestionService
from app.normalization.service import NormalizationService
from app.services.earnings_calendar import EarningsCalendarService


def main() -> None:
    parser = argparse.ArgumentParser(description="Check structured earnings release ingestion + tradeability.")
    parser.add_argument("--ticker", required=True, help="Ticker, e.g. AAPL")
    parser.add_argument("--from", dest="from_date", default="", help="YYYY-MM-DD, default event day - 370d")
    parser.add_argument("--to", dest="to_date", default="", help="YYYY-MM-DD, default today UTC")
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    now = datetime.now(timezone.utc)
    from_date = args.from_date or (now.date() - timedelta(days=370)).isoformat()
    to_date = args.to_date or now.date().isoformat()

    init_db()
    settings = get_settings()
    analysis = AnalysisService(settings)
    normalization = NormalizationService(settings)

    with db_session() as session:
        refresh = EarningsCalendarService(settings).refresh(
            session,
            from_date=from_date,
            to_date=to_date,
            symbols=[ticker],
        )
        items, check = EarningsReleaseClient(settings).build_from_calendar(
            session,
            from_date=from_date,
            to_date=to_date,
            symbols=[ticker],
        )
        persisted = IngestionService(settings).persist_items(session, items, [check])
        clusters = normalization.build_clusters(session, raw_ids=persisted.raw_item_ids)

        latest_cluster = clusters[-1] if clusters else None
        tradeability = None
        earnings_review = None
        if latest_cluster and latest_cluster.canonical.tickers:
            pseudo_event = Event(
                event_type=latest_cluster.canonical.event_type,
                summary=latest_cluster.canonical.summary,
                tickers=latest_cluster.canonical.tickers,
                entities=latest_cluster.canonical.entities,
                severity=latest_cluster.canonical.severity,
                confidence=70,
                validation_status="VALID",
                event_time=latest_cluster.canonical.event_time,
            )
            tradeability = analysis.assess_tradeability(pseudo_event, session=session)
            earnings_review = analysis.build_earnings_review(
                session,
                latest_cluster.canonical.tickers[0],
                latest_cluster.canonical.event_time,
            )

    payload = {
        "ticker": ticker,
        "from_date": from_date,
        "to_date": to_date,
        "calendar_refresh": {
            "fetched": refresh.fetched,
            "upserted": refresh.upserted,
            "skipped": refresh.skipped,
        },
        "source_check": {
            "status": check.status,
            "error_message": check.error_message,
            "details": check.details,
        },
        "persisted": {
            "fetched": persisted.fetched,
            "inserted": persisted.inserted,
            "duplicate_dropped": persisted.duplicate_dropped,
        },
        "latest_cluster": None
        if latest_cluster is None
        else {
            "event_type": latest_cluster.canonical.event_type,
            "tickers": latest_cluster.canonical.tickers,
            "event_time_utc": latest_cluster.canonical.event_time.isoformat(),
            "summary": latest_cluster.canonical.summary,
        },
        "tradeability": tradeability,
        "earnings_review": earnings_review,
        "can_trade_now": bool(tradeability and tradeability.get("tradeable")),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
