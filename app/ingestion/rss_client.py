from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import feedparser
from dateutil import parser as dt_parser

from app.analysis.taxonomy import SOURCE_TIER
from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker relevance: pre-compiled lookup structures built from company_names.py
# ---------------------------------------------------------------------------
try:
    from app.core.company_names import TICKER_TO_COMPANY_ALIASES as _TICKER_ALIASES

    _ALIAS_TO_TICKER: dict[str, str] = {
        name: ticker for ticker, names in _TICKER_ALIASES.items() for name in names
    }
    # Company-name pattern: longest alias first to avoid partial shadowing
    _all_aliases = sorted(_ALIAS_TO_TICKER.keys(), key=len, reverse=True)
    _COMPANY_ALIAS_RE: re.Pattern[str] | None = re.compile(
        r"(" + "|".join(re.escape(n) for n in _all_aliases) + r")",
        re.IGNORECASE,
    )
    # Ticker-symbol pattern: whole-word match only to avoid false positives
    _SP100_TICKER_RE: re.Pattern[str] | None = re.compile(
        r"\b(" + "|".join(re.escape(t) for t in sorted(_TICKER_ALIASES, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
except Exception:  # pragma: no cover – graceful degradation if module unavailable
    _TICKER_ALIASES = {}  # type: ignore[assignment]
    _ALIAS_TO_TICKER = {}
    _COMPANY_ALIAS_RE = None
    _SP100_TICKER_RE = None


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

    def _ticker_relevance(self, title: str, body: str) -> tuple[list[str], int]:
        """Return (matched_tickers, source_tier) based on ticker/company name presence.

        Tier 1 – ticker or company name found in the article title.
        Tier 2 – found only in the first 200 characters of the body.
        Tier 3 – no SP100 match (general market news).
        """
        if _SP100_TICKER_RE is None and _COMPANY_ALIAS_RE is None:
            return [], 3

        title_text = title or ""
        body_snippet = (body or "")[:200]

        # ticker → best (lowest) tier seen so far
        matched: dict[str, int] = {}

        if _SP100_TICKER_RE is not None:
            for m in _SP100_TICKER_RE.finditer(title_text):
                t = m.group(1).upper()
                matched[t] = min(matched.get(t, 3), 1)
            for m in _SP100_TICKER_RE.finditer(body_snippet):
                t = m.group(1).upper()
                if t not in matched:
                    matched[t] = 2

        if _COMPANY_ALIAS_RE is not None:
            for m in _COMPANY_ALIAS_RE.finditer(title_text):
                t = _ALIAS_TO_TICKER.get(m.group(1).lower())
                if t:
                    matched[t] = min(matched.get(t, 3), 1)
            for m in _COMPANY_ALIAS_RE.finditer(body_snippet):
                t = _ALIAS_TO_TICKER.get(m.group(1).lower())
                if t and t not in matched:
                    matched[t] = 2

        if not matched:
            return [], 3

        return list(matched.keys()), min(matched.values())

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
                matched_tickers, relevance_tier = self._ticker_relevance(title, body)
                item_metadata: dict = {"feed": feed_url, "source_quality_tier": tier}
                if matched_tickers:
                    item_metadata["matched_tickers"] = matched_tickers
                items.append(
                    RawNewsItem(
                        source=source_name,
                        url=url,
                        title=title,
                        body=body,
                        published_at=published,
                        ingested_at=utc_now(),
                        hash=item_hash,
                        source_tier=relevance_tier,
                        metadata=item_metadata,
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
