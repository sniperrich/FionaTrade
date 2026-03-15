from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnalystRating, EarningsCalendar, FundamentalsSnapshot


def get_latest_snapshot(session: Session, ticker: str) -> FundamentalsSnapshot | None:
    """Return the most recent fundamentals snapshot for a ticker."""
    return session.execute(
        select(FundamentalsSnapshot)
        .where(FundamentalsSnapshot.ticker == ticker.upper())
        .order_by(FundamentalsSnapshot.fetched_at.desc())
    ).scalars().first()


def get_latest_analyst_rating(session: Session, ticker: str) -> AnalystRating | None:
    """Return the most recent analyst rating for a ticker."""
    return session.execute(
        select(AnalystRating)
        .where(AnalystRating.ticker == ticker.upper())
        .order_by(AnalystRating.fetched_at.desc())
    ).scalars().first()


def get_upcoming_earnings(session: Session, ticker: str, lookahead_days: int = 30) -> dict | None:
    """Return next scheduled earnings date for a ticker, if any."""
    now = datetime.now(timezone.utc)
    row = session.execute(
        select(EarningsCalendar)
        .where(
            EarningsCalendar.symbol == ticker.upper(),
            EarningsCalendar.report_date >= now,
        )
        .order_by(EarningsCalendar.report_date.asc())
    ).scalars().first()

    if not row:
        return None

    days_until = (row.report_date.replace(tzinfo=timezone.utc) - now).days
    if days_until > lookahead_days:
        return None

    return {
        "symbol": row.symbol,
        "report_date": row.report_date.strftime("%Y-%m-%d"),
        "report_hour": row.report_hour,
        "quarter": row.quarter,
        "fiscal_year": row.fiscal_year,
        "eps_estimate": row.eps_estimate,
        "revenue_estimate": row.revenue_estimate,
        "days_until": days_until,
    }


def get_last_earnings(session: Session, ticker: str, limit: int = 4) -> list[dict]:
    """Return the last N earnings results for a ticker."""
    now = datetime.now(timezone.utc)
    rows = session.execute(
        select(EarningsCalendar)
        .where(
            EarningsCalendar.symbol == ticker.upper(),
            EarningsCalendar.report_date < now,
            EarningsCalendar.eps_actual.is_not(None),
        )
        .order_by(EarningsCalendar.report_date.desc())
        .limit(limit)
    ).scalars().all()

    results = []
    for row in rows:
        eps_surprise = None
        if row.eps_actual is not None and row.eps_estimate is not None and row.eps_estimate != 0:
            eps_surprise = round((row.eps_actual - row.eps_estimate) / abs(row.eps_estimate) * 100, 1)

        results.append({
            "report_date": row.report_date.strftime("%Y-%m-%d"),
            "quarter": row.quarter,
            "fiscal_year": row.fiscal_year,
            "eps_actual": row.eps_actual,
            "eps_estimate": row.eps_estimate,
            "eps_surprise_pct": eps_surprise,
            "revenue_actual": row.revenue_actual,
            "revenue_estimate": row.revenue_estimate,
        })
    return results


def build_fundamentals_context_text(session: Session, ticker: str) -> str:
    """Build a compact text block of fundamentals data for LLM prompts."""
    lines: list[str] = [f"=== FUNDAMENTALS FOR {ticker} ==="]

    snap = get_latest_snapshot(session, ticker)
    if snap:
        lines.append(f"Period: {snap.period} ({snap.period_type})")
        if snap.pe_ratio is not None:
            lines.append(f"  P/E: {snap.pe_ratio:.1f}")
        if snap.pb_ratio is not None:
            lines.append(f"  P/B: {snap.pb_ratio:.2f}")
        if snap.roe is not None:
            lines.append(f"  ROE: {snap.roe:.1%}")
        if snap.gross_margin is not None:
            lines.append(f"  Gross Margin: {snap.gross_margin:.1%}")
        if snap.operating_margin is not None:
            lines.append(f"  Operating Margin: {snap.operating_margin:.1%}")
        if snap.debt_to_equity is not None:
            lines.append(f"  Debt/Equity: {snap.debt_to_equity:.2f}")
        if snap.market_cap is not None:
            lines.append(f"  Market Cap: ${snap.market_cap / 1e9:.1f}B")
        if snap.beta is not None:
            lines.append(f"  Beta: {snap.beta:.2f}")
        if snap.week_52_high and snap.week_52_low:
            lines.append(f"  52W Range: ${snap.week_52_low:.2f} – ${snap.week_52_high:.2f}")
    else:
        lines.append("  No fundamentals snapshot available.")

    # Analyst ratings
    rating = get_latest_analyst_rating(session, ticker)
    if rating:
        total = (rating.strong_buy + rating.buy + rating.hold + rating.sell + rating.strong_sell) or 1
        bullish = (rating.strong_buy + rating.buy) / total
        bearish = (rating.sell + rating.strong_sell) / total
        lines.append(
            f"\nAnalyst Ratings ({rating.period}): "
            f"SB={rating.strong_buy} B={rating.buy} H={rating.hold} "
            f"S={rating.sell} SS={rating.strong_sell} "
            f"[{bullish:.0%} bullish / {bearish:.0%} bearish]"
        )
        if rating.target_mean:
            upside = ""
            if rating.last_price_at_fetch and rating.last_price_at_fetch > 0:
                upside = f" ({(rating.target_mean / rating.last_price_at_fetch - 1):.1%} upside)"
            lines.append(f"  Price Target: ${rating.target_mean:.2f}{upside} (range ${rating.target_low:.2f}–${rating.target_high:.2f})")

    # Earnings history
    earnings = get_last_earnings(session, ticker, limit=4)
    if earnings:
        lines.append("\nRecent Earnings:")
        for e in earnings:
            surprise_str = f" ({e['eps_surprise_pct']:+.1f}% surprise)" if e["eps_surprise_pct"] is not None else ""
            lines.append(
                f"  {e['report_date']}: EPS {e['eps_actual']}{surprise_str}"
            )

    upcoming = get_upcoming_earnings(session, ticker)
    if upcoming:
        lines.append(f"\nNext Earnings: {upcoming['report_date']} ({upcoming['days_until']} days, {upcoming['report_hour']})")

    return "\n".join(lines)
