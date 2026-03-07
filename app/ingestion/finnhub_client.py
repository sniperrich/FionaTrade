from __future__ import annotations

import logging

import httpx
from dateutil import parser as dt_parser

from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)


class FinnhubNewsClient:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, limit: int = 50) -> tuple[list[RawNewsItem], SourceCheck]:
        if not self.settings.enable_finnhub:
            return [], SourceCheck(
                source_key="finnhub",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub News",
                status="OFFLINE",
                error_message="Finnhub source disabled by config",
            )

        if not self.settings.finnhub_api_key:
            return [], SourceCheck(
                source_key="finnhub",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub News",
                status="OFFLINE",
                error_message="FINNHUB_API_KEY not configured",
            )

        url = f"{self.BASE_URL}/news"
        params = {
            "category": "general",
            "minId": 0,
            "token": self.settings.finnhub_api_key,
        }
        items: list[RawNewsItem] = []

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Finnhub news fetch failed: %s", exc)
            return [], SourceCheck(
                source_key="finnhub",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub News",
                status="OFFLINE",
                error_message=f"Finnhub request failed: {exc}",
            )

        for row in data[:limit]:
            article_url = row.get("url")
            title = row.get("headline", "")
            body = row.get("summary", "")
            source = (row.get("source") or "finnhub").lower()
            ts = row.get("datetime")
            if not article_url or not title:
                continue

            try:
                published = dt_parser.parse(str(ts)) if isinstance(ts, str) else utc_now()
            except Exception:
                published = utc_now()

            item_hash = make_hash(source, article_url, title)
            items.append(
                RawNewsItem(
                    source=source,
                    url=article_url,
                    title=title,
                    body=body,
                    published_at=published,
                    ingested_at=utc_now(),
                    hash=item_hash,
                    source_tier=2,
                    metadata={"category": "general"},
                )
            )

        return items, SourceCheck(
            source_key="finnhub",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub News",
            status="ONLINE",
            details={"items": len(items)},
        )
