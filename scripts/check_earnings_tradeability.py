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
from app.core.utils import ensure_utc
from app.db.database import db_session, init_db
from app.db.models import Event
from app.services.earnings_calendar import EarningsCalendarService


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return ensure_utc(dt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check earnings fetch + earnings-driven tradeability for a ticker.")
    parser.add_argument("--ticker", required=True, help="Ticker, e.g. AAPL")
    parser.add_argument("--event-time", default=datetime.now(timezone.utc).isoformat(), help="ISO datetime, default now UTC")
    parser.add_argument("--event-type", default="unknown", help="Event type, e.g. earnings_miss / guidance_cut / unknown")
    parser.add_argument(
        "--event-summary",
        default="Quarterly earnings update with management commentary",
        help="Short event summary used for tradeability check",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    event_ts = _parse_dt(args.event_time)
    from_date = (event_ts.date() - timedelta(days=400)).isoformat()
    to_date = (event_ts.date() + timedelta(days=120)).isoformat()

    init_db()
    settings = get_settings()
    analysis = AnalysisService(settings)

    with db_session() as session:
        refresh = EarningsCalendarService(settings).refresh(
            session,
            from_date=from_date,
            to_date=to_date,
            symbols=[ticker],
        )
        event = Event(
            event_type=args.event_type,
            tickers=[ticker],
            entities=[ticker],
            severity=70,
            confidence=70,
            validation_status="VALID",
            summary=args.event_summary,
            event_time=event_ts,
        )

        earnings_context = analysis._earnings_calendar_context(session, ticker, event_ts)
        earnings_review = analysis.build_earnings_review(session, ticker, event_ts)
        tradeability = analysis.assess_tradeability(event, session=session)

    payload = {
        "ticker": ticker,
        "event_time_utc": event_ts.isoformat(),
        "calendar_refresh": {
            "fetched": refresh.fetched,
            "upserted": refresh.upserted,
            "skipped": refresh.skipped,
            "from_date": refresh.from_date,
            "to_date": refresh.to_date,
        },
        "earnings_context": earnings_context,
        "earnings_review": earnings_review,
        "tradeability": tradeability,
        "can_trade_now": bool(
            tradeability.get("tradeable", False)
            and earnings_review is not None
            and earnings_review.get("tradeability") == "GOOD"
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
