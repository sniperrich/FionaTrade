from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Event, EventEvidence, RawItem


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
        # Backtest mode: strict temporal filter — only events that occurred before as_of
        stmt = select(Event).where(
            Event.event_time >= since,
            Event.event_time <= ref_time,
        )
    else:
        # Live mode: also catch recently-ingested events via created_at
        stmt = select(Event).where(
            (Event.event_time >= since) | (Event.created_at >= since)
        )
    if ticker:
        # SQLAlchemy JSON contains check - use a Python-level filter after fetch
        pass
    stmt = stmt.order_by(Event.event_time.desc()).limit(limit * 3 if ticker else limit)

    rows = session.execute(stmt).scalars().all()

    results = []
    for row in rows:
        if ticker and ticker.upper() not in [t.upper() for t in (row.tickers or [])]:
            continue
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
        if len(results) >= limit:
            break

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
    """Return recent RawItems mentioning a ticker directly (from Finnhub or flagged)."""
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)
    ticker_lower = ticker.lower()

    stmt = select(RawItem).where(
        RawItem.published_at >= since, RawItem.source_tier <= 2
    )
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    rows = session.execute(
        stmt.order_by(RawItem.published_at.desc()).limit(500)
    ).scalars().all()

    results = []
    for row in rows:
        text = (row.title + " " + row.body[:200]).lower()
        if ticker_lower in text or f"${ticker_lower}" in text:
            results.append({
                "title": row.title,
                "source": row.source,
                "published_at": row.published_at.isoformat(),
                "body_snippet": row.body[:400],
                "source_tier": row.source_tier,
            })
        if len(results) >= limit:
            break

    return results


def build_news_context_text(
    session: Session, ticker: str, lookback_hours: int = 168, as_of: datetime | None = None,
) -> str:
    """Build a compact text block of recent news/events for LLM prompts.
    
    Default 168h (7 days) lookback to catch weekly ingestion cycles.
    """
    events = get_recent_events(session, ticker=ticker, lookback_hours=lookback_hours, limit=10, as_of=as_of)
    news = get_ticker_news_summary(session, ticker=ticker, lookback_hours=lookback_hours, limit=8, as_of=as_of)

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
