from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MacroIndicator, RawItem


# Series IDs considered key macro indicators (subset used for quick summaries)
_KEY_SERIES = {
    "CPIAUCSL": "CPI Inflation",
    "GDP": "GDP Growth",
    "UNRATE": "Unemployment Rate",
    "FEDFUNDS": "Fed Funds Rate",
    "DGS10": "10Y Treasury Yield",
    "T10Y2Y": "Yield Curve (10Y-2Y)",
    "UMCSENT": "Consumer Sentiment",
    "VIXCLS": "VIX",
}

# Keywords that tag a RawItem as macro-relevant
_MACRO_KEYWORDS = [
    "federal reserve", "fed ", "fomc", "interest rate", "inflation", "cpi", "pce",
    "gdp", "unemployment", "jobs report", "nonfarm payroll", "treasury yield",
    "yield curve", "recession", "economic growth", "rate hike", "rate cut",
    "monetary policy", "fiscal policy", "jerome powell", "treasury secretary",
    "tariff", "trade war", "sanctions", "debt ceiling",
]

# Keywords that flag a RawItem as geopolitical / macro-shock relevant
_GEO_KEYWORDS = [
    "war", "conflict", "military", "strike", "attack", "invasion", "ceasefire",
    "iran", "russia", "china", "ukraine", "nato", "strait", "hormuz",
    "tariff", "trade deal", "sanction", "embargo", "export control",
    "opec", "oil price", "energy crisis", "supply chain",
    "executive order", "white house", "congress", "senate", "legislation",
    "trump", "president", "policy", "election",
    "geopolit", "g7", "g20", "imf", "world bank",
    "nuclear", "missile", "drone", "terrorist",
    "inflation shock", "rate decision", "fed rate",
]

# Sources dedicated to world/geopolitical news
_GEO_SOURCES = {"bbc", "aljazeera", "axios", "npr"}


def get_latest_indicators(
    session: Session, series_ids: list[str] | None = None, as_of: datetime | None = None,
) -> dict[str, dict]:
    """Return the most recent observation for each requested FRED series.

    Returns a dict keyed by series_id:
      {"CPIAUCSL": {"value": 3.4, "date": "2024-12-01", "name": "CPI Inflation"}, ...}

    Args:
        as_of: If set, only return observations on or before this date.
    """
    target_ids = series_ids or list(_KEY_SERIES.keys())

    stmt = select(MacroIndicator).where(
        MacroIndicator.series_id.in_(target_ids)
    )
    if as_of:
        stmt = stmt.where(MacroIndicator.observation_date <= as_of)
    rows = session.execute(
        stmt.order_by(MacroIndicator.series_id, MacroIndicator.observation_date.desc())
    ).scalars().all()

    # Keep only the latest per series
    latest: dict[str, MacroIndicator] = {}
    for row in rows:
        if row.series_id not in latest:
            latest[row.series_id] = row

    return {
        sid: {
            "value": row.value,
            "date": row.observation_date.strftime("%Y-%m-%d"),
            "name": _KEY_SERIES.get(sid, row.indicator_name),
            "unit": row.unit,
        }
        for sid, row in latest.items()
    }


def get_indicator_trend(
    session: Session, series_id: str, lookback_days: int = 90, as_of: datetime | None = None,
) -> list[dict]:
    """Return recent observations for a single FRED series, oldest-first."""
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(days=lookback_days)
    stmt = select(MacroIndicator).where(
        MacroIndicator.series_id == series_id,
        MacroIndicator.observation_date >= since,
    )
    if as_of:
        stmt = stmt.where(MacroIndicator.observation_date <= ref_time)
    rows = session.execute(
        stmt.order_by(MacroIndicator.observation_date.asc())
    ).scalars().all()

    return [
        {"date": r.observation_date.strftime("%Y-%m-%d"), "value": r.value}
        for r in rows
    ]


def get_macro_news_summary(
    session: Session, lookback_hours: int = 48, limit: int = 20, as_of: datetime | None = None,
) -> list[dict]:
    """Return recent macro-relevant RawItems for use as LLM context."""
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)
    stmt = select(RawItem).where(RawItem.published_at >= since)
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    rows = session.execute(
        stmt.order_by(RawItem.published_at.desc()).limit(200)
    ).scalars().all()

    macro_items = []
    for row in rows:
        text = (row.title + " " + row.body[:300]).lower()
        if any(kw in text for kw in _MACRO_KEYWORDS):
            macro_items.append({
                "title": row.title,
                "source": row.source,
                "published_at": row.published_at.isoformat(),
                "body_snippet": row.body[:300],
            })
        if len(macro_items) >= limit:
            break

    return macro_items


def get_geopolitical_news(
    session: Session, lookback_hours: int = 120, limit: int = 20, as_of: datetime | None = None,
) -> list[dict]:
    """Return recent geopolitical / macro-shock news from dedicated world-news sources.

    Queries geo sources (BBC, Al Jazeera, Axios, NPR) directly, then falls back to
    keyword-matching any source for articles that contain geo keywords.
    Returns most-recent-first, capped at `limit`.
    """
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)

    stmt = (
        select(RawItem)
        .where(RawItem.published_at >= since)
        .where(RawItem.source.in_(_GEO_SOURCES))
    )
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    geo_rows = session.execute(
        stmt.order_by(RawItem.published_at.desc()).limit(500)
    ).scalars().all()

    # Filter to only geo-keyword-relevant articles from those sources
    results: list[dict] = []
    for row in geo_rows:
        text = (row.title + " " + row.body[:400]).lower()
        if any(kw in text for kw in _GEO_KEYWORDS):
            results.append({
                "title": row.title,
                "source": row.source,
                "published_at": row.published_at.isoformat(),
                "body_snippet": row.body[:400],
            })
        if len(results) >= limit:
            break

    # If too few geo-source results, supplement with keyword matches from any source
    if len(results) < limit // 2:
        stmt2 = (
            select(RawItem)
            .where(RawItem.published_at >= since)
            .where(RawItem.source.notin_(_GEO_SOURCES))
        )
        if as_of:
            stmt2 = stmt2.where(RawItem.published_at <= ref_time)
        other_rows = session.execute(
            stmt2.order_by(RawItem.published_at.desc()).limit(300)
        ).scalars().all()
        seen_urls = {r["title"] for r in results}
        for row in other_rows:
            text = (row.title + " " + row.body[:400]).lower()
            kw_count = sum(1 for kw in _GEO_KEYWORDS if kw in text)
            if kw_count >= 2 and row.title not in seen_urls:
                results.append({
                    "title": row.title,
                    "source": row.source,
                    "published_at": row.published_at.isoformat(),
                    "body_snippet": row.body[:400],
                })
                seen_urls.add(row.title)
            if len(results) >= limit:
                break

    results.sort(key=lambda x: x["published_at"], reverse=True)
    return results[:limit]


def build_macro_context_text(session: Session, as_of: datetime | None = None) -> str:
    """Build a compact text block describing current macro conditions for LLM prompts."""
    indicators = get_latest_indicators(session, as_of=as_of)
    news = get_macro_news_summary(session, lookback_hours=72, limit=10, as_of=as_of)
    geo = get_geopolitical_news(session, lookback_hours=120, limit=12, as_of=as_of)

    lines: list[str] = ["=== MACRO INDICATORS (FRED) ==="]
    for sid, data in indicators.items():
        val = f"{data['value']:.2f}" if data["value"] is not None else "N/A"
        lines.append(f"  {data['name']}: {val} (as of {data['date']})")

    if geo:
        lines.append("\n=== GEOPOLITICAL & POLICY EVENTS ===")
        lines.append("(wars, sanctions, tariffs, major policy shifts — direct market-shock drivers)")
        for item in geo[:8]:
            pub = item["published_at"][:10]
            snippet = item["body_snippet"][:200].replace("\n", " ")
            lines.append(f"  [{pub}][{item['source']}] {item['title']}")
            if snippet and snippet != item["title"]:
                lines.append(f"    → {snippet}")

    if news:
        lines.append("\n=== RECENT MACRO-ECONOMIC NEWS ===")
        for item in news[:5]:
            lines.append(f"  [{item['source']}] {item['title']}")

    return "\n".join(lines)
