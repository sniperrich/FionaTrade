from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, select, text

from app.core.config import get_settings
from app.core.utils import ensure_utc
from app.db.database import db_session
from app.db.models import Bar1m, Event


def _parse_utc(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit 1m bar coverage for a backtest window.")
    parser.add_argument("--start-date", default="2026-01-02")
    parser.add_argument("--end-date", default="2026-01-10")
    parser.add_argument("--horizon-min", type=int, default=120)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    settings = get_settings()
    start_dt = _parse_utc(args.start_date)
    end_dt = _parse_utc(args.end_date)
    if end_dt <= start_dt:
        raise SystemExit("end_date must be greater than start_date")

    with db_session() as session:
        rows = session.execute(
            text(
                """
                SELECT ticker, COUNT(*) AS bar_count, MIN(ts) AS min_ts, MAX(ts) AS max_ts
                FROM bars_1m
                WHERE ts >= :start_dt AND ts < :end_dt
                GROUP BY ticker
                ORDER BY bar_count ASC, ticker ASC
                """
            ),
            {"start_dt": start_dt, "end_dt": end_dt},
        ).all()

        events = (
            session.execute(
                select(Event)
                .where(and_(Event.event_time >= start_dt, Event.event_time < end_dt))
                .order_by(Event.event_time.asc())
                .limit(args.limit)
            )
            .scalars()
            .all()
        )

        missing_entry: list[dict] = []
        missing_exit: list[dict] = []
        covered = 0

        for event in events:
            if not event.tickers:
                continue
            ticker = str(event.tickers[0]).upper()
            event_ts = ensure_utc(event.event_time)
            entry_ts = event_ts + timedelta(minutes=1)
            exit_ts = event_ts + timedelta(minutes=max(1, args.horizon_min))

            entry_bar = session.execute(
                select(Bar1m.id)
                .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= entry_ts))
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).first()
            if not entry_bar:
                missing_entry.append({"event_id": event.id, "ticker": ticker, "event_time": event_ts.isoformat()})
                continue

            exit_bar = session.execute(
                select(Bar1m.id)
                .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= exit_ts))
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).first()
            if not exit_bar:
                missing_exit.append({"event_id": event.id, "ticker": ticker, "event_time": event_ts.isoformat()})
                continue

            covered += 1

    print(f"[coverage] window={start_dt.isoformat()} -> {end_dt.isoformat()}")
    print(f"[coverage] tickers_with_bars={len(rows)}")
    print(f"[coverage] events_checked={len(events)} covered={covered} missing_entry={len(missing_entry)} missing_exit={len(missing_exit)}")
    print("[coverage] lowest-count tickers:")
    for ticker, bar_count, min_ts, max_ts in rows[:15]:
        print(f"  {ticker:>6} bars={bar_count:>5} min={min_ts} max={max_ts}")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start_date": start_dt.isoformat(), "end_date": end_dt.isoformat()},
        "horizon_min": max(1, args.horizon_min),
        "tickers_with_bars": len(rows),
        "bar_coverage_rows": [
            {"ticker": row[0], "bar_count": int(row[1]), "min_ts": str(row[2]), "max_ts": str(row[3])}
            for row in rows
        ],
        "events_checked": len(events),
        "events_covered": covered,
        "missing_entry": missing_entry,
        "missing_exit": missing_exit,
        "min_trade_confidence": settings.min_trade_confidence,
    }

    out_dir = Path(settings.log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"bar_coverage_{ts}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[coverage] saved: {out_path}")


if __name__ == "__main__":
    main()
