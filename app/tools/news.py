from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import Event, EventEvidence, RawItem

# Map tickers to company name search terms for broader news matching.
# Only include names distinctive enough to avoid false positives.
_TICKER_COMPANY_NAMES: dict[str, list[str]] = {
    "AAPL": ["Apple"],
    "ABBV": ["AbbVie"],
    "ABT": ["Abbott Lab"],
    "ACN": ["Accenture"],
    "ADBE": ["Adobe"],
    "AIG": ["AIG"],
    "AMD": ["AMD"],
    "AMGN": ["Amgen"],
    "AMZN": ["Amazon"],
    "AVGO": ["Broadcom"],
    "AXP": ["American Express"],
    "BA": ["Boeing"],
    "BAC": ["Bank of America"],
    "BLK": ["BlackRock"],
    "BMY": ["Bristol-Myers"],
    "BRK.B": ["Berkshire"],
    "C": ["Citigroup", "Citibank"],
    "CAT": ["Caterpillar"],
    "CHTR": ["Charter Comm"],
    "CL": ["Colgate"],
    "CMCSA": ["Comcast"],
    "COP": ["ConocoPhillips"],
    "COST": ["Costco"],
    "CRM": ["Salesforce"],
    "CSCO": ["Cisco"],
    "CVS": ["CVS Health"],
    "CVX": ["Chevron"],
    "DE": ["Deere"],
    "DHR": ["Danaher"],
    "DIS": ["Disney"],
    "DUK": ["Duke Energy"],
    "EMR": ["Emerson"],
    "FDX": ["FedEx"],
    "GD": ["General Dynamics"],
    "GE": ["GE Aerospace"],
    "GILD": ["Gilead"],
    "GM": ["General Motors"],
    "GOOG": ["Alphabet", "Google"],
    "GOOGL": ["Alphabet", "Google"],
    "GS": ["Goldman Sachs"],
    "HD": ["Home Depot"],
    "HON": ["Honeywell"],
    "IBM": ["IBM"],
    "INTC": ["Intel"],
    "INTU": ["Intuit"],
    "ISRG": ["Intuitive Surgical"],
    "JNJ": ["Johnson & Johnson", "J&J"],
    "JPM": ["JPMorgan", "JP Morgan"],
    "KO": ["Coca-Cola"],
    "LIN": ["Linde"],
    "LLY": ["Eli Lilly"],
    "LMT": ["Lockheed Martin"],
    "LOW": ["Lowe's"],
    "MA": ["Mastercard"],
    "MCD": ["McDonald"],
    "MDLZ": ["Mondelez"],
    "MDT": ["Medtronic"],
    "MET": ["MetLife"],
    "META": ["Meta Platform"],
    "MMM": ["3M "],
    "MO": ["Altria"],
    "MRK": ["Merck"],
    "MS": ["Morgan Stanley"],
    "MSFT": ["Microsoft"],
    "NEE": ["NextEra"],
    "NFLX": ["Netflix"],
    "NKE": ["Nike"],
    "NOW": ["ServiceNow"],
    "NVDA": ["Nvidia", "NVIDIA"],
    "ORCL": ["Oracle"],
    "PEP": ["PepsiCo", "Pepsi"],
    "PFE": ["Pfizer"],
    "PG": ["Procter & Gamble", "P&G"],
    "PM": ["Philip Morris"],
    "PYPL": ["PayPal"],
    "QCOM": ["Qualcomm"],
    "RTX": ["Raytheon"],
    "SBUX": ["Starbucks"],
    "SCHW": ["Schwab"],
    "SO": ["Southern Co"],
    "SPG": ["Simon Property"],
    "T": ["AT&T"],
    "TGT": ["Target "],
    "TMO": ["Thermo Fisher"],
    "TMUS": ["T-Mobile"],
    "TSLA": ["Tesla"],
    "TXN": ["Texas Instruments"],
    "UNH": ["UnitedHealth"],
    "UNP": ["Union Pacific"],
    "UPS": ["UPS "],
    "USB": ["U.S. Bancorp"],
    "V": ["Visa"],
    "VZ": ["Verizon"],
    "WBA": ["Walgreens"],
    "WFC": ["Wells Fargo"],
    "WMT": ["Walmart"],
    "XOM": ["Exxon", "ExxonMobil"],
}


def get_recent_events(
    session: Session,
    ticker: str | None = None,
    lookback_hours: int = 48,
    limit: int = 30,
    min_confidence: int = 0,
    as_of: datetime | None = None,
) -> list[dict]:
    """Return recent events, optionally filtered by ticker.

    Each dict has: id, event_type, tickers, severity, confidence, event_time,
    validation_status, summary, evidence_count.

    Args:
        as_of: Reference time for temporal filtering (backtest mode).
               When set, only events with event_time <= as_of are returned.
    """
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)

    if as_of:
        stmt = select(Event).where(
            Event.event_time >= since,
            Event.event_time <= ref_time,
        )
    else:
        stmt = select(Event).where(
            (Event.event_time >= since) | (Event.created_at >= since)
        )

    # SQL-level ticker filter using JSON text matching (works for SQLite + Postgres)
    if ticker:
        stmt = stmt.where(Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%'))

    stmt = stmt.order_by(Event.event_time.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()

    results = []
    for row in rows:
        if row.confidence < min_confidence:
            continue

        evidence_count = session.execute(
            select(EventEvidence).where(EventEvidence.event_id == row.id)
        ).scalars().all()

        results.append({
            "id": row.id,
            "event_type": row.event_type,
            "tickers": row.tickers,
            "severity": row.severity,
            "confidence": row.confidence,
            "event_time": row.event_time.isoformat(),
            "validation_status": row.validation_status,
            "summary": row.summary,
            "evidence_count": len(evidence_count),
        })

    return results


def get_event_detail(session: Session, event_id: int) -> dict | None:
    """Return full event detail including all evidence."""
    event = session.get(Event, event_id)
    if not event:
        return None

    evidence_rows = session.execute(
        select(EventEvidence).where(EventEvidence.event_id == event_id)
    ).scalars().all()

    return {
        "id": event.id,
        "event_type": event.event_type,
        "tickers": event.tickers,
        "severity": event.severity,
        "confidence": event.confidence,
        "event_time": event.event_time.isoformat(),
        "validation_status": event.validation_status,
        "summary": event.summary,
        "evidence": [
            {
                "url": e.url,
                "source": e.source,
                "source_tier": e.source_tier,
                "captured_at": e.captured_at.isoformat(),
                "summary": e.summary,
            }
            for e in evidence_rows
        ],
    }


def get_ticker_news_summary(
    session: Session, ticker: str, lookback_hours: int = 72, limit: int = 15,
    as_of: datetime | None = None,
) -> list[dict]:
    """Return recent RawItems mentioning a ticker directly.

    Uses word-boundary matching for ticker symbols AND company name matching
    via _TICKER_COMPANY_NAMES to maximize recall while avoiding false positives.
    Also searches metadata_json for SEC/Finnhub ticker tags.
    """
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)
    ticker_upper = ticker.upper()

    # Word-boundary patterns for ticker symbol
    title_patterns = [
        f"({ticker_upper})%",   # (AAPL)...
        f"% {ticker_upper} %",  # ... AAPL ...
        f"% {ticker_upper},%",  # ... AAPL,...
        f"% {ticker_upper}:%",  # ... AAPL:...
        f"% {ticker_upper}'%",  # ... AAPL's...
        f"{ticker_upper} %",    # AAPL ... (start of title)
        f"% {ticker_upper}",    # ... AAPL (end of title)
    ]

    # Company name patterns (catches "Apple", "Amazon", etc.)
    company_names = _TICKER_COMPANY_NAMES.get(ticker_upper, [])
    for name in company_names:
        title_patterns.append(f"%{name}%")

    # Metadata exact match for SEC/Finnhub tagged items
    metadata_pattern = f'%"ticker": "{ticker_upper}"%'

    title_conditions = [RawItem.title.ilike(p) for p in title_patterns]

    stmt = select(RawItem).where(
        RawItem.published_at >= since,
        sa.or_(
            *title_conditions,
            RawItem.metadata_json.ilike(metadata_pattern),
        ),
    )
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    rows = session.execute(
        stmt.order_by(RawItem.source_tier.asc(), RawItem.published_at.desc()).limit(limit)
    ).scalars().all()

    results = []
    for row in rows:
        results.append({
            "title": row.title,
            "source": row.source,
            "published_at": row.published_at.isoformat(),
            "body_snippet": row.body[:400],
            "source_tier": row.source_tier,
        })

    return results


def build_news_context_text(
    session: Session, ticker: str, lookback_hours: int = 336, as_of: datetime | None = None,
) -> str:
    """Build a compact text block of recent news/events for LLM prompts.
    
    Default 336h (14 days) lookback to ensure adequate news coverage.
    """
    events = get_recent_events(session, ticker=ticker, lookback_hours=lookback_hours, limit=15, as_of=as_of)
    news = get_ticker_news_summary(session, ticker=ticker, lookback_hours=lookback_hours, limit=10, as_of=as_of)

    lines: list[str] = [f"=== RECENT EVENTS FOR {ticker} ==="]
    if events:
        for ev in events:
            lines.append(
                f"  [{ev['event_time'][:16]}] {ev['event_type']} "
                f"(conf={ev['confidence']}, sev={ev['severity']}): {ev['summary'][:150]}"
            )
    else:
        lines.append("  No recent events found.")

    lines.append(f"\n=== RECENT NEWS FOR {ticker} ===")
    if news:
        for item in news:
            lines.append(f"  [{item['source']}] {item['title']}")
    else:
        lines.append("  No recent news found.")

    return "\n".join(lines)
