from __future__ import annotations

import pytest

from app.market.backfill import MarketBackfillService


def test_market_backfill_requires_finnhub_key(session, settings):
    settings.market_backfill_allow_stooq_fallback = False
    svc = MarketBackfillService(settings)
    with pytest.raises(ValueError, match="FINNHUB_API_KEY is not configured"):
        svc.run(session, start_date="2026-01-01", end_date="2026-02-01")
