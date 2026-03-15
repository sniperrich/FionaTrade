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


def get_latest_indicators(session: Session, series_ids: list[str] | None = None) -> dict[str, dict]:
    """Return the most recent observation for each requested FRED series.

    Returns a dict keyed by series_id:
      {"CPIAUCSL": {"value": 3.4, "date": "2024-12-01", "name": "CPI Inflation"}, ...}
    """
    target_ids = series_ids or list(_KEY_SERIES.keys())

    rows = session.execute(
        select(MacroIndicator)
        .where(MacroIndicator.series_id.in_(target_ids))
        .order_by(MacroIndicator.series_id, MacroIndicator.observation_date.desc())
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
    session: Session, series_id: str, lookback_days: int = 90
) -> list[dict]:
    """Return recent observations for a single FRED series, oldest-first."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    rows = session.execute(
        select(MacroIndicator)
        .where(
            MacroIndicator.series_id == series_id,
            MacroIndicator.observation_date >= since,
        )
        .order_by(MacroIndicator.observation_date.asc())
    ).scalars().all()

    return [
        {"date": r.observation_date.strftime("%Y-%m-%d"), "value": r.value}
        for r in rows
    ]


def get_macro_news_summary(
    session: Session, lookback_hours: int = 48, limit: int = 20
) -> list[dict]:
    """Return recent macro-relevant RawItems for use as LLM context."""
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    rows = session.execute(
        select(RawItem)
        .where(RawItem.published_at >= since)
        .order_by(RawItem.published_at.desc())
        .limit(200)
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


def build_macro_context_text(session: Session) -> str:
    """Build a compact text block describing current macro conditions for LLM prompts."""
    indicators = get_latest_indicators(session)
    news = get_macro_news_summary(session, lookback_hours=72, limit=10)

    lines: list[str] = ["=== MACRO INDICATORS (FRED) ==="]
    for sid, data in indicators.items():
        val = f"{data['value']:.2f}" if data["value"] is not None else "N/A"
        lines.append(f"  {data['name']}: {val} (as of {data['date']})")

    if news:
        lines.append("\n=== RECENT MACRO NEWS ===")
        for item in news[:5]:
            lines.append(f"  [{item['source']}] {item['title']}")

    return "\n".join(lines)
