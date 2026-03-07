from __future__ import annotations

import logging
from urllib.parse import urlparse

import feedparser
from dateutil import parser as dt_parser

from app.analysis.taxonomy import SOURCE_TIER
from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)


class RssClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _source_name(self, feed_url: str) -> str:
        host = urlparse(feed_url).netloc.lower()
        if "reuters" in host:
            return "reuters"
        if "bloomberg" in host:
            return "bloomberg"
        if "wsj" in host:
            return "wsj"
        if "ft.com" in host:
            return "ft"
        if "cnbc" in host:
            return "cnbc"
        if "marketwatch" in host:
            return "marketwatch"
        if "seekingalpha" in host:
            return "seekingalpha"
        return "rss"

    def _feed_status_key(self, feed_url: str) -> tuple[str, str, str]:
        host = urlparse(feed_url).netloc.lower()
        source_name = self._source_name(feed_url)
        source_key = f"rss:{host}"
        display_name = f"RSS {source_name.upper()} ({host})"
        return source_key, source_name, display_name

    def fetch(self) -> tuple[list[RawNewsItem], list[SourceCheck]]:
        if not self.settings.enable_rss:
            checks: list[SourceCheck] = []
            for feed_url in self.settings.rss_sources:
                source_key, source_name, display_name = self._feed_status_key(feed_url)
                checks.append(
                    SourceCheck(
                        source_key=source_key,
                        source_name=source_name,
                        source_type="rss",
                        display_name=display_name,
                        status="OFFLINE",
                        error_message="RSS source disabled by config",
                        details={"feed": feed_url},
                    )
                )
            return [], checks

        items: list[RawNewsItem] = []
        checks: list[SourceCheck] = []

        for feed_url in self.settings.rss_sources:
            source_key, source_name, display_name = self._feed_status_key(feed_url)
            tier = SOURCE_TIER.get(source_name, 2)

            try:
                feed = feedparser.parse(feed_url)
            except Exception as exc:
                logger.warning("RSS parse failed %s: %s", feed_url, exc)
                checks.append(
                    SourceCheck(
                        source_key=source_key,
                        source_name=source_name,
                        source_type="rss",
                        display_name=display_name,
                        status="OFFLINE",
                        error_message=f"RSS parse failed: {exc}",
                        details={"feed": feed_url},
                    )
                )
                continue

            feed_items = 0
            for entry in feed.entries[:60]:
                url = entry.get("link")
                title = entry.get("title", "").strip()
                body = entry.get("summary", "") or entry.get("description", "")
                if not url or not title:
                    continue

                published_raw = (
                    entry.get("published")
                    or entry.get("updated")
                    or entry.get("pubDate")
                    or utc_now().isoformat()
                )
                try:
                    published = dt_parser.parse(published_raw)
                except Exception:
                    published = utc_now()

                item_hash = make_hash(source_name, url, title)
                items.append(
                    RawNewsItem(
                        source=source_name,
                        url=url,
                        title=title,
                        body=body,
                        published_at=published,
                        ingested_at=utc_now(),
                        hash=item_hash,
                        source_tier=tier,
                        metadata={"feed": feed_url},
                    )
                )
                feed_items += 1

            bozo_error = None
            if getattr(feed, "bozo", False):
                exc = getattr(feed, "bozo_exception", None)
                bozo_error = str(exc) if exc else "Malformed feed"

            if bozo_error and feed_items == 0:
                checks.append(
                    SourceCheck(
                        source_key=source_key,
                        source_name=source_name,
                        source_type="rss",
                        display_name=display_name,
                        status="OFFLINE",
                        error_message=bozo_error,
                        details={"feed": feed_url, "items": feed_items},
                    )
                )
            else:
                checks.append(
                    SourceCheck(
                        source_key=source_key,
                        source_name=source_name,
                        source_type="rss",
                        display_name=display_name,
                        status="ONLINE",
                        details={"feed": feed_url, "items": feed_items},
                    )
                )

        return items, checks
