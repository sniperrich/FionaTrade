from __future__ import annotations

import html as _html_module
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import feedparser
import httpx
from dateutil import parser as dt_parser

from app.analysis.taxonomy import SOURCE_TIER, is_secondary_confirmation_source, normalize_source_name
from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML stripping — used for sources like Axios that return full HTML bodies
# ---------------------------------------------------------------------------
class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        self._chunks.append(data)

    def get_text(self) -> str:
        return " ".join(self._chunks)


def _strip_html(text: str) -> str:
    """Strip HTML tags and unescape entities. Returns plain text."""
    if not text or "<" not in text:
        return _html_module.unescape(text or "")
    s = _TagStripper()
    s.feed(_html_module.unescape(text))
    return re.sub(r"\s+", " ", s.get_text()).strip()


# Geo/world-news sources whose bodies need HTML-stripping (Axios returns full HTML)
_HTML_BODY_SOURCES = {"axios.com", "api.axios.com"}

# Pre-filter keywords for geo sources: articles that don't contain any of these
# are pure noise (celebrity/sports/culture) and are skipped before DB write.
# Axios is excluded (100% relevant, full articles). BBC Business is excluded
# (economically scoped feed, all articles market-relevant).
_GEO_INGEST_KW = [
    "war", "military", "strike", "attack", "bomb", "missile", "ceasefire",
    "iran", "russia", "china", "ukraine", "nato", "israel", "syria", "korea",
    "tariff", "sanction", "embargo", "export ban", "trade deal", "trade war",
    "opec", "oil", "energy", "gas price", "hormuz",
    "trump", "white house", "congress", "senate", "executive order", "legislation",
    "federal reserve", "fed rate", "interest rate", "inflation", "gdp", "recession",
    "policy", "g7", "g20", "imf", "central bank",
    "nuclear", "weapon", "defence", "defense", "pentagon",
    "economy", "market", "stock", "investor", "financial",
    "wage", "employment", "jobs", "unemployment", "hiring",
    "mortgage", "housing", "retail sales", "consumer", "borrowing",
    "supply chain", "shipping", "port", "freight",
]
# Sources that always pass (no pre-filter needed — either 100% relevant or topic-scoped)
_GEO_NO_FILTER_HOSTS = {
    "api.axios.com", "axios.com",     # Axios: 100% relevant
    # BBC sub-feeds that are already topic-scoped (business, tech) don't need geo-filtering
    # BBC World and BBC US&Canada go through the filter (general news feeds)
}

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

    @staticmethod
    def _load_feed(feed_url: str):
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(feed_url)
            resp.raise_for_status()
            return feedparser.parse(resp.content)

    def _source_name(self, feed_url: str) -> str:
        host = urlparse(feed_url).netloc.lower()
        if "reuters" in host:
            return "reuters"
        if "bloomberg" in host:
            return "bloomberg"
        if "wsj" in host or "dj.com" in host or "barrons" in host:
            return "wsj"
        if "ft.com" in host:
            return "ft"
        if "nytimes" in host:
            return "nytimes"
        if "cnbc" in host:
            return "cnbc"
        if "marketwatch" in host:
            return "marketwatch"
        if "yahoo" in host:
            return "yahoo_finance"
        if "thestreet" in host:
            return "thestreet"
        if "seekingalpha" in host:
            return "seekingalpha"
        if "bbc" in host:
            return "bbc"
        if "aljazeera" in host:
            return "aljazeera"
        if "axios" in host:
            return "axios"
        if "npr" in host:
            return "npr"
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
                feed = self._load_feed(feed_url)
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
            feed_host = urlparse(feed_url).netloc.lower()
            needs_html_strip = any(h in feed_host for h in _HTML_BODY_SOURCES)
            is_geo_source = source_name in {"bbc", "aljazeera", "axios", "npr"}
            apply_geo_filter = is_geo_source and feed_host not in _GEO_NO_FILTER_HOSTS
            for entry in feed.entries[:60]:
                url = entry.get("link")
                title = entry.get("title", "").strip()
                raw_body = entry.get("summary", "") or entry.get("description", "")
                body = _strip_html(raw_body) if needs_html_strip else raw_body
                if not url or not title:
                    continue

                # Drop pure noise from general-news geo sources (celebrity, sports, culture)
                if apply_geo_filter:
                    search_text = (title + " " + body[:300]).lower()
                    if not any(kw in search_text for kw in _GEO_INGEST_KW):
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

                normalized_source = normalize_source_name(source_name)
                item_hash = make_hash(normalized_source, url, title)
                matched_tickers, relevance_tier = self._ticker_relevance(title, body)
                item_metadata: dict = {"feed": feed_url, "source_quality_tier": tier}
                if matched_tickers:
                    item_metadata["matched_tickers"] = matched_tickers
                if source_name in {"bbc", "aljazeera", "axios", "npr"}:
                    item_metadata["category"] = "geopolitical"
                source_tier = relevance_tier
                if is_secondary_confirmation_source(normalized_source):
                    source_tier = max(source_tier, 2)
                items.append(
                    RawNewsItem(
                        source=normalized_source,
                        url=url,
                        title=title,
                        body=body,
                        published_at=published,
                        ingested_at=utc_now(),
                        hash=item_hash,
                        source_tier=source_tier,
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

    def fetch_ticker_news(
        self, tickers: list[str]
    ) -> tuple[list[RawNewsItem], list[SourceCheck]]:
        """Fetch per-ticker RSS headlines from Yahoo Finance.

        Yields ticker-tagged items from Yahoo Finance, but they remain
        secondary-confirmation evidence rather than primary trigger sources.
        """
        if not self.settings.enable_rss or not self.settings.enable_ticker_rss:
            return [], []

        all_items: list[RawNewsItem] = []
        ticker_counts: dict[str, int] = {}

        for ticker in tickers:
            feed_url = f"https://finance.yahoo.com/rss/headline?s={ticker}"
            try:
                feed = self._load_feed(feed_url)
            except Exception as exc:
                logger.warning("Yahoo Finance ticker RSS failed %s: %s", ticker, exc)
                continue

            count = 0
            for entry in feed.entries[:20]:
                url = entry.get("link")
                title = (entry.get("title") or "").strip()
                body = entry.get("summary", "") or entry.get("description", "") or ""
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

                item_hash = make_hash("yahoo_finance", url, title)
                all_items.append(
                    RawNewsItem(
                        source="yahoo_finance",
                        url=url,
                        title=title,
                        body=body,
                        published_at=published,
                        ingested_at=utc_now(),
                        hash=item_hash,
                        source_tier=2,
                        metadata={"ticker": ticker, "feed": feed_url},
                    )
                )
                count += 1
            if count:
                ticker_counts[ticker] = count

        total = sum(ticker_counts.values())
        if total == 0:
            return [], []

        check = SourceCheck(
            source_key="yahoo_finance_ticker_rss",
            source_name="yahoo_finance",
            source_type="rss",
            display_name="Yahoo Finance Ticker RSS",
            status="ONLINE",
            details={"items": total, "tickers": len(ticker_counts)},
        )
        logger.info("Yahoo Finance ticker RSS: %d items for %d tickers", total, len(ticker_counts))
        return all_items, [check]
