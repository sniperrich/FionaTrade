from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import sleep

import httpx
from dateutil import parser as dt_parser

from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)

_RATE_SLEEP = 0.4  # 150 calls/min limit → safe at 0.4s


class FinnhubNewsClient:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _offline(self, reason: str) -> tuple[list[RawNewsItem], SourceCheck]:
        return [], SourceCheck(
            source_key="finnhub",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub News",
            status="OFFLINE",
            error_message=reason,
        )

    def _parse_item(self, row: dict, ticker: str | None = None) -> RawNewsItem | None:
        article_url = row.get("url")
        title = (row.get("headline") or "").strip()
        if not article_url or not title:
            return None
        body = row.get("summary", "") or ""
        source = (row.get("source") or "finnhub").lower()
        ts = row.get("datetime")
        try:
            if isinstance(ts, (int, float)):
                published = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            elif isinstance(ts, str):
                published = dt_parser.parse(ts)
            else:
                published = utc_now()
        except Exception:
            published = utc_now()

        meta: dict = {"category": row.get("category", "general")}
        if ticker:
            meta["ticker"] = ticker

        return RawNewsItem(
            source=source,
            url=article_url,
            title=title,
            body=body,
            published_at=published,
            ingested_at=utc_now(),
            hash=make_hash(source, article_url, title),
            source_tier=1,  # ticker-specific news → tier 1
            metadata=meta,
        )

    def fetch(self, limit: int = 50) -> tuple[list[RawNewsItem], SourceCheck]:
        """Fetch general market news (merger/general categories)."""
        if not self.settings.enable_finnhub:
            return self._offline("Finnhub source disabled by config")
        if not self.settings.finnhub_api_key:
            return self._offline("FINNHUB_API_KEY not configured")

        items: list[RawNewsItem] = []
        try:
            with httpx.Client(timeout=15.0) as client:
                for category in ("general", "merger"):
                    resp = client.get(
                        f"{self.BASE_URL}/news",
                        params={"category": category, "minId": 0, "token": self.settings.finnhub_api_key},
                    )
                    if resp.status_code != 200:
                        logger.warning("Finnhub /news category=%s status=%s", category, resp.status_code)
                        continue
                    for row in resp.json()[:limit]:
                        item = self._parse_item(row)
                        if item:
                            item.source_tier = 2  # general news → tier 2
                            items.append(item)
                    sleep(_RATE_SLEEP)
        except Exception as exc:
            logger.warning("Finnhub news fetch failed: %s", exc)
            return self._offline(f"Finnhub request failed: {exc}")

        return items, SourceCheck(
            source_key="finnhub",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub News",
            status="ONLINE",
            details={"items": len(items)},
        )

    def fetch_company_news(
        self,
        tickers: list[str],
        from_date: str,
        to_date: str,
    ) -> tuple[list[RawNewsItem], SourceCheck]:
        """Fetch ticker-specific news from /company-news (Basic plan: 1yr history).

        Args:
            tickers: List of US equity symbols (e.g. ['AAPL', 'MSFT']).
            from_date: Start date string YYYY-MM-DD.
            to_date: End date string YYYY-MM-DD (inclusive).
        """
        if not self.settings.enable_finnhub:
            return self._offline("Finnhub source disabled by config")
        if not self.settings.finnhub_api_key:
            return self._offline("FINNHUB_API_KEY not configured")

        items: list[RawNewsItem] = []
        errors: list[str] = []

        with httpx.Client(timeout=15.0) as client:
            for ticker in tickers:
                try:
                    resp = client.get(
                        f"{self.BASE_URL}/company-news",
                        params={
                            "symbol": ticker,
                            "from": from_date,
                            "to": to_date,
                            "token": self.settings.finnhub_api_key,
                        },
                    )
                    if resp.status_code == 429:
                        logger.warning("Finnhub rate limit on company-news ticker=%s", ticker)
                        sleep(2.0)
                        errors.append(f"{ticker}:429")
                        continue
                    if resp.status_code != 200:
                        logger.warning("Finnhub company-news ticker=%s status=%s", ticker, resp.status_code)
                        errors.append(f"{ticker}:{resp.status_code}")
                        sleep(_RATE_SLEEP)
                        continue

                    for row in resp.json():
                        item = self._parse_item(row, ticker=ticker)
                        if item:
                            items.append(item)

                    logger.debug("Finnhub company-news ticker=%s count=%s", ticker, len(resp.json()))
                except Exception as exc:
                    logger.warning("Finnhub company-news ticker=%s error: %s", ticker, exc)
                    errors.append(f"{ticker}:error")

                sleep(_RATE_SLEEP)

        status = "ONLINE" if len(errors) < len(tickers) else "OFFLINE"
        return items, SourceCheck(
            source_key="finnhub",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub Company News",
            status=status,
            details={"items": len(items), "tickers": len(tickers), "errors": errors},
        )
