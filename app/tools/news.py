from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.analysis.taxonomy import normalize_source_name
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


def _metadata_ticker_pattern_clause(ticker_upper: str):
    pattern = f'%\"ticker\": \"{ticker_upper}\"%'
    return sa.cast(RawItem.metadata_json, sa.Text).ilike(pattern)


def get_recent_events(
    session: Session,
    ticker: str | None = None,
    lookback_hours: int = 48,
    limit: int = 30,
    min_confidence: int = 0,
    as_of: datetime | None = None,
    since: datetime | None = None,
    allowed_sources: list[str] | None = None,
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
    allowed_source_set = {
        normalize_source_name(str(source).strip().lower())
        for source in (allowed_sources or [])
        if str(source).strip()
    }

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

    if since:
        stmt = stmt.where(sa.or_(Event.event_time >= since, Event.created_at >= since))

    stmt = stmt.order_by(Event.event_time.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()

    results = []
    for row in rows:
        if row.confidence < min_confidence:
            continue

        evidence_count = session.execute(
            select(EventEvidence).where(EventEvidence.event_id == row.id)
        ).scalars().all()
        if allowed_source_set:
            event_sources = {
                normalize_source_name(e.source)
                for e in evidence_count
                if getattr(e, "source", None)
            }
            if not event_sources.intersection(allowed_source_set):
                continue

        results.append({
            "id": row.id,
            "event_type": row.event_type,
            "tickers": row.tickers,
            "severity": row.severity,
            "confidence": row.confidence,
            "event_time": row.event_time.isoformat(),
            "created_at": row.created_at.isoformat() if row.created_at else None,
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
    session: Session, ticker: str, lookback_hours: int = 72, limit: int = 25,
    as_of: datetime | None = None,
    since: datetime | None = None,
    allowed_sources: list[str] | None = None,
) -> list[dict]:
    """Return recent RawItems mentioning a ticker directly.

    Uses word-boundary matching for ticker symbols AND company name matching
    via _TICKER_COMPANY_NAMES to maximize recall while avoiding false positives.
    Also searches metadata_json for SEC/Finnhub/Yahoo ticker tags, and body text
    for company name mentions.
    """
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)
    ticker_upper = ticker.upper()
    allowed_source_set = {
        normalize_source_name(str(source).strip().lower())
        for source in (allowed_sources or [])
        if str(source).strip()
    }

    # Word-boundary patterns for ticker symbol in title
    title_patterns = [
        f"({ticker_upper})%",   # (AAPL)...
        f"% {ticker_upper} %",  # ... AAPL ...
        f"% {ticker_upper},%",  # ... AAPL,...
        f"% {ticker_upper}:%",  # ... AAPL:...
        f"% {ticker_upper}'%",  # ... AAPL's...
        f"{ticker_upper} %",    # AAPL ... (start of title)
        f"% {ticker_upper}",    # ... AAPL (end of title)
    ]

    # Company name patterns (catches "Apple", "Amazon", etc.) in title and body
    company_names = _TICKER_COMPANY_NAMES.get(ticker_upper, [])
    for name in company_names:
        title_patterns.append(f"%{name}%")

    title_conditions = [RawItem.title.ilike(p) for p in title_patterns]

    # Body text matching for company names (first 600 chars to keep it efficient)
    body_conditions = []
    for name in company_names:
        body_conditions.append(sa.func.substr(RawItem.body, 1, 600).ilike(f"%{name}%"))

    all_conditions = [*title_conditions, _metadata_ticker_pattern_clause(ticker_upper)]
    if body_conditions:
        all_conditions.extend(body_conditions)

    stmt = select(RawItem).where(
        RawItem.published_at >= since,
        sa.or_(*all_conditions),
    )
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    rows = session.execute(
        stmt.order_by(RawItem.source_tier.asc(), RawItem.published_at.desc()).limit(limit)
    ).scalars().all()

    results = []
    for row in rows:
        source_name = normalize_source_name(row.source)
        if allowed_source_set and source_name not in allowed_source_set:
            continue
        ingested_at = row.ingested_at.isoformat() if row.ingested_at else None
        published_at = row.published_at.isoformat()
        if since:
            ingested_dt = _parse_dt(ingested_at)
            published_dt = _parse_dt(published_at)
            if (
                (ingested_dt is not None and ingested_dt < since)
                and (published_dt is not None and published_dt < since)
            ):
                continue
        results.append({
            "id": row.id,
            "title": row.title,
            "source": source_name,
            "published_at": published_at,
            "ingested_at": ingested_at,
            "body_snippet": (row.body or "")[:400],
            "body_full": row.body or "",
            "source_tier": row.source_tier,
        })

    return results


def count_new_raw_items(session: Session, since: datetime) -> int:
    """Count RawItems ingested after `since` (used for cycle freshness gate)."""
    return session.execute(
        select(sa.func.count()).select_from(RawItem).where(RawItem.ingested_at >= since)
    ).scalar_one()


def get_articles_full_text(session: Session, item_ids: list[int]) -> dict[int, dict]:
    """Fetch full body text for a list of RawItem IDs.

    Returns a dict mapping id → {title, source, published_at, body}.
    """
    if not item_ids:
        return {}
    rows = session.execute(
        select(RawItem).where(RawItem.id.in_(item_ids))
    ).scalars().all()
    return {
        row.id: {
            "title": row.title,
            "source": row.source,
            "published_at": row.published_at.isoformat(),
            "body": row.body or "",
        }
        for row in rows
    }


def _age_label(hours: float) -> str:
    """Return a recency label for news context formatting."""
    if hours <= 24:
        return "🔴 BREAKING"
    elif hours <= 72:
        return "🟡 RECENT"
    elif hours <= 168:
        return "🟢 THIS WEEK"
    else:
        return "⚪ OLDER"


def _hours_since(ts_str: str, ref_time: datetime) -> float:
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (ref_time - ts).total_seconds() / 3600)
    except Exception:
        return 999.0


def _parse_dt(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def build_news_screening_text(
    session: Session,
    ticker: str,
    lookback_hours: int = 336,
    as_of: datetime | None = None,
    since: datetime | None = None,
    allowed_sources: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Build a numbered screening list with article IDs for the pre-screening pass.

    Args:
        since: When provided (live trading mode), articles ingested before this
               timestamp are shown as "(already analyzed)" background context —
               the screener is instructed NOT to request full reads for them.
               Articles ingested after `since` are marked ⭐ NEW.

    Returns (screening_text, raw_news_list).
    """
    ref_time = as_of or datetime.now(timezone.utc)
    news = get_ticker_news_summary(
        session,
        ticker=ticker,
        lookback_hours=lookback_hours,
        limit=25,
        as_of=as_of,
        allowed_sources=allowed_sources,
    )

    # Partition articles: new (unseen) vs already-analyzed
    new_articles = []
    old_articles = []
    for item in news:
        ingested = _parse_dt(item.get("ingested_at"))
        if since and ingested and ingested < since:
            old_articles.append(item)
        else:
            new_articles.append(item)

    has_new = bool(new_articles)
    lines: list[str] = [
        f"=== ARTICLE SCREENING LIST FOR {ticker} ===",
    ]

    if since:
        lines.append(
            f"⭐ NEW articles (ingested since last run): {len(new_articles)}  |  "
            f"Old/already-analyzed: {len(old_articles)}"
        )
        lines.append(
            "IMPORTANT: Only request full_read for ⭐ NEW articles. "
            "Old articles were visible in the previous agent run."
        )
    else:
        lines.append("(Read headlines and snippets. Return which article IDs are worth reading in full.)")

    lines.append("(🔴=<24h  🟡=1-3d  🟢=3-7d  ⚪=7-14d  |  tier1=top source, tier3=low quality)")
    lines.append("")

    # NEW articles first
    if new_articles:
        lines.append("── ⭐ NEW SINCE LAST RUN ──" if since else "── ARTICLES ──")
        for item in new_articles:
            age = _hours_since(item["published_at"], ref_time)
            label = _age_label(age)
            tier_tag = f"[tier{item['source_tier']}]"
            snippet = (item.get("body_snippet") or "").strip().replace("\n", " ")[:180]
            lines.append(f"  ⭐ id={item['id']}  {label} {tier_tag} [{item['source']}]")
            lines.append(f"     {item['title']}")
            if snippet and snippet.strip() != item["title"].strip():
                lines.append(f"     → {snippet}")
            lines.append("")
    elif since:
        lines.append("── ⭐ NEW SINCE LAST RUN: (none) ──")
        lines.append("")

    # OLD articles — shown as background only
    if old_articles and since:
        lines.append("── 📚 BACKGROUND (already analyzed in previous run — do NOT request full read) ──")
        for item in old_articles[:8]:  # cap old articles to save tokens
            age = _hours_since(item["published_at"], ref_time)
            label = _age_label(age)
            lines.append(f"  [OLD] id={item['id']}  {label} [{item['source']}] {item['title']}")
        if len(old_articles) > 8:
            lines.append(f"  ... and {len(old_articles) - 8} more older articles")
        lines.append("")

    if not news:
        lines.append("  (no articles found)")



    return "\n".join(lines), news


def build_news_context_text(
    session: Session,
    ticker: str,
    lookback_hours: int = 336,
    as_of: datetime | None = None,
    since: datetime | None = None,
    expanded_articles: dict[int, dict] | None = None,
    allowed_sources: list[str] | None = None,
) -> str:
    """Build a time-bucketed news context block for LLM prompts.

    News is organized into recency tiers so the agent can weight recent catalysts
    more heavily than background context. Labels:
      🔴 BREAKING  = within 24h
      🟡 RECENT    = 1–3 days
      🟢 THIS WEEK = 3–7 days
      ⚪ OLDER     = 7–14 days

    Args:
        expanded_articles: Optional dict from get_articles_full_text() — when provided,
            these articles are injected as full-text blocks below the headline list.
    """
    ref_time = as_of or datetime.now(timezone.utc)

    events = get_recent_events(
        session,
        ticker=ticker,
        lookback_hours=lookback_hours,
        limit=25,
        as_of=as_of,
        since=since,
        allowed_sources=allowed_sources,
    )
    news = get_ticker_news_summary(
        session,
        ticker=ticker,
        lookback_hours=lookback_hours,
        limit=20,
        as_of=as_of,
        since=since,
        allowed_sources=allowed_sources,
    )

    lines: list[str] = [f"=== NEWS & EVENTS FOR {ticker} ==="]
    lines.append("(🔴=<24h  🟡=1-3d  🟢=3-7d  ⚪=7-14d)")

    lines.append("\n[VALIDATED EVENTS — clustered & confidence-scored]")
    if events:
        for ev in events:
            age = _hours_since(ev["event_time"], ref_time)
            label = _age_label(age)
            summary = (ev.get("summary") or "")[:200]
            lines.append(
                f"  {label} [{ev['event_time'][:16]}] {ev['event_type']} "
                f"conf={ev['confidence']} sev={ev['severity']}: {summary}"
            )
    else:
        lines.append("  (none)")

    lines.append("\n[RAW HEADLINES — direct from sources]")
    if news:
        for item in news:
            age = _hours_since(item["published_at"], ref_time)
            label = _age_label(age)
            body = (item.get("body_snippet") or "").strip().replace("\n", " ")[:200]
            tier_tag = f"[tier{item['source_tier']}]"
            line = f"  {label} {tier_tag} [{item['source']}] {item['title']}"
            if body:
                line += f"\n    → {body}"
            lines.append(line)
    else:
        lines.append("  (none)")

    # Inject full-text blocks for articles the agent requested to read in full
    if expanded_articles:
        lines.append("\n[FULL ARTICLE TEXT — you requested these for deeper reading]")
        for art_id, art in expanded_articles.items():
            pub = art["published_at"][:16]
            full_body = (art["body"] or "").strip()[:2500]
            lines.append(f"\n📖 [FULL] id={art_id} [{art['source']}] {art['title']} ({pub})")
            lines.append("-" * 60)
            lines.append(full_body)
            lines.append("-" * 60)

    return "\n".join(lines)
