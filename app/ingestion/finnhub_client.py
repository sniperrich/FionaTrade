from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from time import sleep

import httpx
from dateutil import parser as dt_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import is_secondary_confirmation_source, normalize_source_name
from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.db.models import AnalystRating, EarningsCalendar, FundamentalsSnapshot
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

logger = logging.getLogger(__name__)

_RATE_SLEEP = 0.4  # 150 calls/min limit → safe at 0.4s

# ---------------------------------------------------------------------------
# Company aliases for relevance filtering: ticker → lowercase search terms
# ---------------------------------------------------------------------------
try:
    from app.core.company_names import TICKER_TO_COMPANY_ALIASES as _TICKER_ALIASES_RAW
    _TICKER_ALIASES: dict[str, list[str]] = {
        t: [a.lower() for a in aliases]
        for t, aliases in _TICKER_ALIASES_RAW.items()
    }
except Exception:
    _TICKER_ALIASES = {}


def _is_relevant(ticker: str, title: str, body: str) -> bool:
    """Return True if the article is meaningfully about this ticker.

    Checks (in order of cost):
    1. Ticker symbol appears as a word/token in title or body
    2. Any company alias (e.g. "Apple", "Nvidia") appears in title or body

    An article tagged by Finnhub that doesn't mention the company by name
    or ticker is classified as noise and discarded.

    Special case: pure uppercase acronym matches (e.g. "AAPL" matching the
    American Association for Physician Leadership) are validated by requiring
    at least one company alias to also appear; if no alias is known the ticker
    match alone is sufficient.
    """
    ticker_upper = ticker.upper()
    search_text = (title + " " + body[:600]).lower()
    title_lower = title.lower()

    aliases = _TICKER_ALIASES.get(ticker_upper, [])

    # Ticker symbol — word-boundary match (avoid "AMD" matching "amended")
    ticker_pat = re.compile(r"\b" + re.escape(ticker_upper.lower()) + r"\b")
    ticker_in_text = bool(ticker_pat.search(search_text))

    # Company name aliases
    alias_in_text = any(alias in search_text for alias in aliases)

    if alias_in_text:
        return True

    # Ticker symbol found but no alias: accept only if we have no aliases defined
    # (avoids acronym collisions like AAPL = medical org when we know "apple" alias)
    if ticker_in_text and not aliases:
        return True

    # Ticker found AND we have aliases — require alias to also appear to avoid
    # acronym collision (e.g. "AAPL" used for non-Apple org)
    if ticker_in_text and aliases:
        # Exception: if ticker is literally in the headline and body is non-empty
        # and the body corroborates the company context, it's likely valid.
        # Use a looser check: accept if body has some financial/market language
        # alongside the ticker mention.
        FINANCE_KW = ["stock", "share", "market cap", "investor", "ceo", "revenue",
                      "earnings", "nasdaq", "nyse", "analyst", "quarter", "fiscal"]
        has_finance = any(kw in search_text for kw in FINANCE_KW)
        return has_finance

    return False


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

    def _parse_item(self, row: dict, ticker: str | None = None, check_relevance: bool = False) -> RawNewsItem | None:
        article_url = row.get("url")
        title = (row.get("headline") or "").strip()
        if not article_url or not title:
            return None
        import html as _html
        body = _html.unescape(row.get("summary", "") or "")
        # Strip bodies that are just a URL (Benzinga sometimes sends a redirect URL as body)
        if body.startswith("http") and " " not in body.strip():
            body = ""
        source = normalize_source_name((row.get("source") or "finnhub").lower())
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

        # Relevance filter: drop articles that don't mention the ticker/company
        if check_relevance and ticker and not _is_relevant(ticker, title, body):
            return None

        meta: dict = {"category": row.get("category", "general")}
        if ticker:
            meta["ticker"] = ticker

        source_tier = 1
        if is_secondary_confirmation_source(source):
            source_tier = 2

        return RawNewsItem(
            source=source,
            url=article_url,
            title=title,
            body=body,
            published_at=published,
            ingested_at=utc_now(),
            hash=make_hash(source, article_url, title),
            source_tier=source_tier,
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

        Applies relevance filtering: articles that don't mention the ticker symbol
        or company name (in headline or first 600 chars of body) are discarded.
        This removes ~60-79% Yahoo noise articles that Finnhub incorrectly tags.

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
        total_raw = 0
        total_filtered = 0

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

                    raw_rows = resp.json()
                    total_raw += len(raw_rows)
                    ticker_items = 0
                    for row in raw_rows:
                        item = self._parse_item(row, ticker=ticker, check_relevance=True)
                        if item:
                            items.append(item)
                            ticker_items += 1

                    filtered_out = len(raw_rows) - ticker_items
                    total_filtered += filtered_out
                    logger.debug(
                        "Finnhub company-news ticker=%s raw=%d kept=%d filtered=%d",
                        ticker, len(raw_rows), ticker_items, filtered_out,
                    )
                except Exception as exc:
                    logger.warning("Finnhub company-news ticker=%s error: %s", ticker, exc)
                    errors.append(f"{ticker}:error")

                sleep(_RATE_SLEEP)

        if total_raw > 0:
            logger.info(
                "Finnhub company-news: raw=%d kept=%d filtered_noise=%d (%.0f%%)",
                total_raw, len(items), total_filtered, total_filtered / total_raw * 100,
            )

        status = "ONLINE" if len(errors) < len(tickers) else "OFFLINE"
        return items, SourceCheck(
            source_key="finnhub",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub Company News",
            status=status,
            details={"items": len(items), "tickers": len(tickers), "errors": errors},
        )

    def fetch_news_sentiment(self, ticker: str) -> dict | None:
        """Fetch aggregated news sentiment scores from Finnhub /news-sentiment.

        Returns a dict with keys: bullish_pct, bearish_pct, sentiment_score,
        buzz_score, weekly_avg_mentions, articles_this_week, company_news_score.

        Returns None if:
        - API key not configured or Finnhub disabled
        - HTTP 403 (endpoint requires Finnhub Premium plan)
        - Any network/parse error

        NOTE: This is a live API call — not backtest-safe. Only called when
        context["as_of"] is not set (live/paper mode only).
        """
        if not self.settings.enable_finnhub or not self.settings.finnhub_api_key:
            return None
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/news-sentiment",
                    params={"symbol": ticker, "token": self.settings.finnhub_api_key},
                )
                if resp.status_code != 200:
                    logger.debug("Finnhub news-sentiment ticker=%s status=%s", ticker, resp.status_code)
                    return None
                data = resp.json()
                sentiment_data = data.get("sentiment") or {}
                buzz_data = data.get("buzz") or {}
                return {
                    "bullish_pct": round(sentiment_data.get("bullishPercent", 0) * 100),
                    "bearish_pct": round(sentiment_data.get("bearishPercent", 0) * 100),
                    "sentiment_score": round(sentiment_data.get("score", 0), 3),
                    "buzz_score": round(buzz_data.get("buzz", 0), 3),
                    "weekly_avg_mentions": buzz_data.get("weeklyAverage", 0),
                    "articles_this_week": buzz_data.get("articlesInLastWeek", 0),
                    "company_news_score": round(data.get("companyNewsScore", 0), 3),
                }
        except Exception as exc:
            logger.debug("Finnhub news-sentiment ticker=%s error: %s", ticker, exc)
            return None

    def fetch_earnings_calendar(
        self,
        from_date: str,
        to_date: str,
        symbols: list[str] | None = None,
    ) -> tuple[list[dict], SourceCheck]:
        """Fetch earnings calendar rows and filter to the configured SP100 universe."""
        if not self.settings.enable_finnhub:
            return [], SourceCheck(
                source_key="finnhub_earnings_calendar",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub Earnings Calendar",
                status="OFFLINE",
                error_message="Finnhub source disabled by config",
            )
        if not self.settings.finnhub_api_key:
            return [], SourceCheck(
                source_key="finnhub_earnings_calendar",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub Earnings Calendar",
                status="OFFLINE",
                error_message="FINNHUB_API_KEY not configured",
            )

        allowed = {ticker.upper() for ticker in self.settings.sp100_tickers}
        if symbols:
            allowed &= {ticker.upper() for ticker in symbols}

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/calendar/earnings",
                    params={
                        "from": from_date,
                        "to": to_date,
                        "token": self.settings.finnhub_api_key,
                    },
                )
                if resp.status_code != 200:
                    return [], SourceCheck(
                        source_key="finnhub_earnings_calendar",
                        source_name="finnhub",
                        source_type="finnhub",
                        display_name="Finnhub Earnings Calendar",
                        status="OFFLINE",
                        error_message=f"Finnhub earnings calendar status={resp.status_code}",
                    )
                payload = resp.json()
        except Exception as exc:
            logger.warning("Finnhub earnings calendar fetch failed: %s", exc)
            return [], SourceCheck(
                source_key="finnhub_earnings_calendar",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub Earnings Calendar",
                status="OFFLINE",
                error_message=f"Finnhub earnings calendar failed: {exc}",
            )

        rows = payload.get("earningsCalendar") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            rows = []

        filtered = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol or symbol not in allowed:
                continue
            filtered.append(row)

        return filtered, SourceCheck(
            source_key="finnhub_earnings_calendar",
            source_name="finnhub",
            source_type="finnhub",
            display_name="Finnhub Earnings Calendar",
            status="ONLINE",
            details={"items": len(filtered), "from": from_date, "to": to_date},
        )


class FinnhubClient:
    """Fundamentals and analyst-ratings client for Finnhub."""

    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _throttle(self) -> None:
        sleep(_RATE_SLEEP)

    # ── Raw fetch helpers ─────────────────────────────────────────────────────

    def get_basic_metrics(self, ticker: str) -> dict | None:
        """GET /stock/metric?symbol={ticker}&metric=all → raw ``metric`` dict, or None on error."""
        if not self.settings.finnhub_api_key:
            logger.warning("get_basic_metrics: FINNHUB_API_KEY not configured")
            return None
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/stock/metric",
                    params={"symbol": ticker, "metric": "all", "token": self.settings.finnhub_api_key},
                )
                if resp.status_code == 429:
                    logger.warning("Finnhub rate limit on /stock/metric ticker=%s", ticker)
                    return None
                if resp.status_code != 200:
                    logger.warning("Finnhub /stock/metric ticker=%s status=%s", ticker, resp.status_code)
                    return None
                payload = resp.json()
                return payload.get("metric") if isinstance(payload, dict) else None
        except Exception as exc:
            logger.warning("Finnhub get_basic_metrics ticker=%s error: %s", ticker, exc)
            return None

    def get_analyst_recommendations(self, ticker: str) -> list[dict]:
        """GET /stock/recommendation?symbol={ticker} → list of recommendation dicts, or [] on error."""
        if not self.settings.finnhub_api_key:
            logger.warning("get_analyst_recommendations: FINNHUB_API_KEY not configured")
            return []
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/stock/recommendation",
                    params={"symbol": ticker, "token": self.settings.finnhub_api_key},
                )
                if resp.status_code == 429:
                    logger.warning("Finnhub rate limit on /stock/recommendation ticker=%s", ticker)
                    return []
                if resp.status_code != 200:
                    logger.warning("Finnhub /stock/recommendation ticker=%s status=%s", ticker, resp.status_code)
                    return []
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Finnhub get_analyst_recommendations ticker=%s error: %s", ticker, exc)
            return []

    def get_price_target(self, ticker: str) -> dict | None:
        """GET /stock/price-target?symbol={ticker} → price-target dict, or None on error."""
        if not self.settings.finnhub_api_key:
            logger.warning("get_price_target: FINNHUB_API_KEY not configured")
            return None
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/stock/price-target",
                    params={"symbol": ticker, "token": self.settings.finnhub_api_key},
                )
                if resp.status_code == 429:
                    logger.warning("Finnhub rate limit on /stock/price-target ticker=%s", ticker)
                    return None
                if resp.status_code != 200:
                    logger.warning("Finnhub /stock/price-target ticker=%s status=%s", ticker, resp.status_code)
                    return None
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Finnhub get_price_target ticker=%s error: %s", ticker, exc)
            return None

    # ── DB upsert helpers ─────────────────────────────────────────────────────

    def upsert_fundamentals_snapshot(self, session: Session, ticker: str) -> bool:
        """Fetch /stock/metric and upsert a FundamentalsSnapshot for the current quarter.

        Also pulls eps/revenue actuals from the most recent EarningsCalendar row.
        Returns True on success, False on failure.
        """
        metrics = self.get_basic_metrics(ticker)
        if not metrics:
            logger.warning("upsert_fundamentals_snapshot: no metrics for ticker=%s", ticker)
            return False

        now = utc_now()
        q = (now.month - 1) // 3 + 1
        period = f"{now.year}Q{q}"

        ec_stmt = (
            select(EarningsCalendar)
            .where(EarningsCalendar.symbol == ticker)
            .order_by(EarningsCalendar.report_date.desc())
            .limit(1)
        )
        ec: EarningsCalendar | None = session.execute(ec_stmt).scalar_one_or_none()

        try:
            with session.begin_nested():
                snap_stmt = select(FundamentalsSnapshot).where(
                    FundamentalsSnapshot.ticker == ticker,
                    FundamentalsSnapshot.period == period,
                    FundamentalsSnapshot.period_type == "quarterly",
                )
                snap = session.execute(snap_stmt).scalar_one_or_none()

                fields: dict = {
                    "pe_ratio": metrics.get("peNormalizedAnnual"),
                    "pb_ratio": metrics.get("pbAnnual"),
                    "ps_ratio": metrics.get("psAnnual"),
                    "roe": metrics.get("roeAnnual"),
                    "roa": metrics.get("roaAnnual"),
                    "gross_margin": metrics.get("grossMarginAnnual"),
                    "operating_margin": metrics.get("operatingMarginAnnual"),
                    "debt_to_equity": metrics.get("totalDebt/totalEquityAnnual"),
                    "current_ratio": metrics.get("currentRatioAnnual"),
                    "market_cap": metrics.get("marketCapitalization"),
                    "beta": metrics.get("beta"),
                    "week_52_high": metrics.get("52WeekHigh"),
                    "week_52_low": metrics.get("52WeekLow"),
                    "eps_actual": ec.eps_actual if ec else None,
                    "eps_estimate": ec.eps_estimate if ec else None,
                    "revenue_actual": ec.revenue_actual if ec else None,
                    "revenue_estimate": ec.revenue_estimate if ec else None,
                    "source": "finnhub",
                    "fetched_at": now,
                    "updated_at": now,
                }

                if snap:
                    for k, v in fields.items():
                        setattr(snap, k, v)
                else:
                    snap = FundamentalsSnapshot(
                        ticker=ticker,
                        period=period,
                        period_type="quarterly",
                        **fields,
                    )
                    session.add(snap)

            logger.debug("upsert_fundamentals_snapshot ticker=%s period=%s", ticker, period)
            return True
        except Exception as exc:
            logger.warning("upsert_fundamentals_snapshot ticker=%s error: %s", ticker, exc)
            return False

    def upsert_analyst_ratings(self, session: Session, ticker: str) -> bool:
        """Fetch /stock/recommendation and /stock/price-target, upsert into AnalystRating.

        Uses the most recent recommendation period. Returns True on success, False on failure.
        """
        recommendations = self.get_analyst_recommendations(ticker)
        self._throttle()
        price_target = self.get_price_target(ticker)

        if not recommendations:
            logger.warning("upsert_analyst_ratings: no recommendations for ticker=%s", ticker)
            return False

        rec = recommendations[0]
        period = rec.get("period", "")
        if not period:
            logger.warning("upsert_analyst_ratings: missing period in recommendations for ticker=%s", ticker)
            return False

        now = utc_now()
        try:
            with session.begin_nested():
                stmt = select(AnalystRating).where(
                    AnalystRating.ticker == ticker,
                    AnalystRating.period == period,
                )
                rating = session.execute(stmt).scalar_one_or_none()

                fields: dict = {
                    "strong_buy": rec.get("strongBuy", 0),
                    "buy": rec.get("buy", 0),
                    "hold": rec.get("hold", 0),
                    "sell": rec.get("sell", 0),
                    "strong_sell": rec.get("strongSell", 0),
                    "target_high": price_target.get("targetHigh") if price_target else None,
                    "target_low": price_target.get("targetLow") if price_target else None,
                    "target_mean": price_target.get("targetMean") if price_target else None,
                    "target_median": price_target.get("targetMedian") if price_target else None,
                    "last_price_at_fetch": None,
                    "source": "finnhub",
                    "fetched_at": now,
                    "updated_at": now,
                }

                if rating:
                    for k, v in fields.items():
                        setattr(rating, k, v)
                else:
                    rating = AnalystRating(
                        ticker=ticker,
                        period=period,
                        **fields,
                    )
                    session.add(rating)

            logger.debug("upsert_analyst_ratings ticker=%s period=%s", ticker, period)
            return True
        except Exception as exc:
            logger.warning("upsert_analyst_ratings ticker=%s error: %s", ticker, exc)
            return False

    # ── Batch refresh ─────────────────────────────────────────────────────────

    def refresh_fundamentals_batch(self, session: Session, tickers: list[str]) -> dict:
        """Upsert fundamentals and analyst ratings for each ticker.

        Throttles at least 0.4 s between every API call.
        Returns ``{"tickers_updated": N, "tickers_failed": N}``.
        """
        updated = 0
        failed = 0
        for ticker in tickers:
            ok_fund = self.upsert_fundamentals_snapshot(session, ticker)
            self._throttle()
            ok_analyst = self.upsert_analyst_ratings(session, ticker)
            self._throttle()
            if ok_fund and ok_analyst:
                updated += 1
            else:
                failed += 1
            logger.debug(
                "refresh_fundamentals_batch ticker=%s fundamentals=%s analyst=%s",
                ticker, ok_fund, ok_analyst,
            )
        return {"tickers_updated": updated, "tickers_failed": failed}
