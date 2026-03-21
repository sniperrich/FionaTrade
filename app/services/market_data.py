from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.broker.alpaca import AlpacaBroker
from app.core.config import Settings
from app.core.utils import utc_now
from app.db.models import Bar1m
from app.market.backfill import MarketBackfillService
from app.services.worker_runtime import WorkerRuntimeService

_NY = ZoneInfo("America/New_York")


class MarketDataService:
    CACHE_FRESH_MINUTES = 360.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtime = WorkerRuntimeService()

    def tracked_tickers(self, selected_ticker: str | None = None) -> list[str]:
        configured = [t.upper() for t in (self.settings.live_trading_tickers or list(self.settings.agent_tickers_override or [])) if t]
        if configured:
            return configured
        if selected_ticker:
            return [selected_ticker.upper()]
        return ["AAPL"]

    def load_analysis_rows(
        self,
        session: Session,
        ticker: str,
        lookback_bars: int = 390,
        end_time: datetime | None = None,
        regular_hours_only: bool = True,
    ) -> list[Bar1m]:
        normalized = ticker.upper()
        end = self._ensure_utc(end_time) or utc_now()
        fetch_bars = int(lookback_bars * 1.8) if regular_hours_only else lookback_bars
        est_trading_days = max(1, fetch_bars // 390)
        buffer_days = max(3, est_trading_days * 2 + 2)
        start = end - timedelta(days=buffer_days)
        rows = (
            session.execute(
                select(Bar1m)
                .where(Bar1m.ticker == normalized, Bar1m.ts >= start, Bar1m.ts <= end)
                .order_by(Bar1m.ts.asc())
                .limit(fetch_bars + 200)
            )
            .scalars()
            .all()
        )
        if not rows and end_time is None:
            rows = (
                session.execute(
                    select(Bar1m)
                    .where(Bar1m.ticker == normalized)
                    .order_by(Bar1m.ts.desc())
                    .limit(fetch_bars + 200)
                )
                .scalars()
                .all()
            )
            rows = list(reversed(rows))
        if regular_hours_only:
            rows = [row for row in rows if self._is_regular_hours(row.ts)]
        return rows[-lookback_bars:]

    def get_chart_bars(
        self,
        session: Session,
        ticker: str,
        timeframe: str = "5Min",
        source: str = "auto",
        limit: int = 78,
    ) -> dict[str, Any]:
        normalized_ticker = ticker.upper()
        requested_source = source.lower().strip()
        cached_payload = self.load_cached_bars(session, normalized_ticker, timeframe, limit)
        cache_is_fresh = self.is_cache_fresh(cached_payload)

        if requested_source == "cache":
            return {
                **cached_payload,
                "source_requested": requested_source,
                "resolved_source": "cache",
            }
        if requested_source == "auto" and cached_payload["count"] and cache_is_fresh:
            return {
                **cached_payload,
                "source_requested": requested_source,
                "resolved_source": "cache",
            }

        broker = AlpacaBroker(self.settings)
        try:
            bars = broker.get_bars(normalized_ticker, timeframe=timeframe, limit=limit)
            return {
                "ticker": normalized_ticker,
                "timeframe": timeframe,
                "count": len(bars),
                "source_requested": requested_source,
                "resolved_source": "broker",
                "cache_row_count": cached_payload.get("cache_row_count", 0),
                "cache_last_ts": cached_payload.get("cache_last_ts"),
                "cache_age_minutes": cached_payload.get("cache_age_minutes"),
                "source_counts": cached_payload.get("source_counts", {}),
                "bars": [
                    {
                        "t": bar.get("t"),
                        "o": float(bar.get("o", 0.0)),
                        "h": float(bar.get("h", 0.0)),
                        "l": float(bar.get("l", 0.0)),
                        "c": float(bar.get("c", 0.0)),
                        "v": float(bar.get("v", 0.0)),
                    }
                    for bar in bars
                ],
            }
        except Exception as exc:
            if requested_source == "auto" and cached_payload["count"]:
                return {
                    **cached_payload,
                    "source_requested": requested_source,
                    "resolved_source": "cache_fallback",
                    "broker_error": str(exc),
                }
            raise

    def load_cached_bars(self, session: Session, ticker: str, timeframe: str, limit: int) -> dict[str, Any]:
        minutes = self.timeframe_to_minutes(timeframe)
        row_limit = min(max(limit * max(minutes, 1) * 3, limit * 20), 25000)
        rows = (
            session.execute(
                select(Bar1m)
                .where(Bar1m.ticker == ticker.upper())
                .order_by(desc(Bar1m.ts))
                .limit(row_limit)
            )
            .scalars()
            .all()
        )
        rows = list(reversed(rows))
        bars = self.aggregate_bars(rows, timeframe, limit)
        latest_ts = self._ensure_utc(rows[-1].ts) if rows else None
        age_minutes = None
        if latest_ts is not None:
            age_minutes = round((utc_now() - latest_ts).total_seconds() / 60.0, 1)
        return {
            "ticker": ticker.upper(),
            "timeframe": timeframe,
            "count": len(bars),
            "bars": bars,
            "resolved_source": "cache",
            "cache_row_count": len(rows),
            "cache_last_ts": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else None,
            "cache_age_minutes": age_minutes,
            "source_counts": dict(Counter(row.source for row in rows)),
        }

    def get_bar_cache_status(self, session: Session, selected_ticker: str) -> dict[str, Any]:
        configured = [t.upper() for t in (self.settings.live_trading_tickers or list(self.settings.agent_tickers_override or []))]
        tracked = configured or [selected_ticker.upper()]
        now = utc_now()
        selected = selected_ticker.upper()
        per_ticker: list[dict[str, Any]] = []
        fresh_count = 0
        latest_global_ts: datetime | None = None
        for ticker in tracked:
            latest_ts = session.execute(select(func.max(Bar1m.ts)).where(Bar1m.ticker == ticker)).scalar_one_or_none()
            latest_ts = self._ensure_utc(latest_ts)
            row_count = session.execute(select(func.count(Bar1m.id)).where(Bar1m.ticker == ticker)).scalar_one()
            age_hours = None
            status = "empty"
            if latest_ts:
                age_hours = round((now - latest_ts).total_seconds() / 3600.0, 2)
                latest_global_ts = max(latest_global_ts, latest_ts) if latest_global_ts else latest_ts
                if age_hours <= 4:
                    status = "fresh"
                    fresh_count += 1
                elif age_hours <= 24:
                    status = "stale"
                else:
                    status = "very_stale"
            per_ticker.append(
                {
                    "ticker": ticker,
                    "row_count": int(row_count),
                    "last_ts": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else None,
                    "age_hours": age_hours,
                    "status": status,
                }
            )

        selected_sources = (
            session.execute(
                select(Bar1m.source, func.count(Bar1m.id))
                .where(Bar1m.ticker == selected)
                .group_by(Bar1m.source)
                .order_by(func.count(Bar1m.id).desc())
            )
            .all()
        )
        selected_summary = next((item for item in per_ticker if item["ticker"] == selected), {
            "ticker": selected,
            "row_count": 0,
            "last_ts": None,
            "age_hours": None,
            "status": "empty",
        })
        return {
            "selected_ticker": selected,
            "configured_tickers_count": len(configured),
            "configured_tickers": configured,
            "using_fallback_ticker": not bool(configured),
            "selected": {
                **selected_summary,
                "source_counts": {source: count for source, count in selected_sources},
            },
            "tracked_count": len(tracked),
            "fresh_count": fresh_count,
            "latest_global_ts": latest_global_ts.isoformat().replace("+00:00", "Z") if latest_global_ts else None,
            "tickers": per_ticker,
        }

    def refresh_bars(
        self,
        session: Session,
        *,
        start_date: str,
        end_date: str,
        tickers: list[str] | None = None,
        chunk_days: int = 5,
        sleep_seconds: float = 0.12,
        trigger: str = "scheduled",
        run_type: str = "bar_backfill",
    ) -> dict[str, Any]:
        normalized_tickers = [ticker.upper() for ticker in (tickers or []) if ticker]
        tracked = normalized_tickers or self.tracked_tickers()
        run = self.runtime.start_run(
            session,
            run_type=run_type,
            trigger=trigger,
            stage="fetching",
            status="RUNNING",
            total_tickers=len(tracked),
            completed_tickers=0,
        )
        self.runtime.add_event(
            session,
            run_type,
            f"Started {trigger} bar refresh for {len(tracked)} tickers",
            run=run,
            stage="fetching",
            payload={"tickers": tracked, "start_date": start_date, "end_date": end_date},
        )
        session.commit()

        def progress(update: dict[str, Any]) -> None:
            self.runtime.update_run(
                session,
                run,
                stage=str(update.get("stage") or run.stage),
                current_ticker=update.get("current_ticker"),
                completed_tickers=int(update.get("completed_tickers") or 0),
                total_tickers=int(update.get("total_tickers") or run.total_tickers),
            )
            if update.get("message"):
                self.runtime.add_event(
                    session,
                    run_type,
                    str(update["message"]),
                    run=run,
                    stage=update.get("stage"),
                    ticker=update.get("current_ticker"),
                    payload=update,
                )
            session.commit()

        try:
            result = MarketBackfillService(self.settings).run(
                session,
                start_date=start_date,
                end_date=end_date,
                tickers=tracked,
                chunk_days=chunk_days,
                sleep_seconds=sleep_seconds,
                progress_callback=progress,
            )
            payload = result.to_dict()
            payload["run_key"] = run.run_key
            self.runtime.finish_run(
                session,
                run,
                status="COMPLETED",
                summary=payload,
                stage="completed",
                current_ticker=None,
                completed_tickers=len(tracked),
            )
            self.runtime.add_event(
                session,
                run_type,
                f"Completed {trigger} bar refresh: +{result.bars_inserted} bars",
                run=run,
                stage="completed",
                payload=payload,
            )
            session.commit()
            return payload
        except Exception as exc:
            self.runtime.finish_run(
                session,
                run,
                status="ERROR",
                error_message=str(exc),
                summary={"error": str(exc)},
                stage="error",
                current_ticker=None,
            )
            self.runtime.add_event(
                session,
                run_type,
                f"{trigger} bar refresh failed: {exc}",
                run=run,
                level="error",
                stage="error",
                payload={"error": str(exc)},
            )
            session.commit()
            raise

    def startup_backfill_if_stale(self, session: Session, trigger: str = "startup") -> dict[str, Any]:
        latest_ts = session.execute(select(func.max(Bar1m.ts))).scalar_one_or_none()
        now_utc = utc_now()
        tickers = self.tracked_tickers()
        if latest_ts is not None:
            latest_ts = self._ensure_utc(latest_ts)
        if latest_ts is not None:
            staleness_hours = (now_utc - latest_ts).total_seconds() / 3600.0
            if staleness_hours < 4:
                payload = {
                    "fresh_skip": True,
                    "staleness_hours": round(staleness_hours, 2),
                    "tickers": tickers,
                }
                run = self.runtime.start_run(
                    session,
                    run_type="bar_backfill",
                    trigger=trigger,
                    stage="fresh_skip",
                    status="COMPLETED",
                    total_tickers=len(tickers),
                    completed_tickers=len(tickers),
                    summary_json=payload,
                )
                self.runtime.finish_run(
                    session,
                    run,
                    status="COMPLETED",
                    stage="fresh_skip",
                    summary=payload,
                    current_ticker=None,
                    completed_tickers=len(tickers),
                )
                self.runtime.add_event(
                    session,
                    "bar_backfill",
                    f"Skipped startup bar refresh: cache is fresh ({staleness_hours:.1f}h old)",
                    run=run,
                    stage="fresh_skip",
                    payload=payload,
                )
                session.commit()
                return payload
            start_date = (latest_ts - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start_date = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d")
        end_date = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
        return self.refresh_bars(
            session,
            start_date=start_date,
            end_date=end_date,
            tickers=tickers,
            chunk_days=5,
            sleep_seconds=0.2,
            trigger=trigger,
        )

    @staticmethod
    def timeframe_to_minutes(timeframe: str) -> int:
        mapping = {
            "1Min": 1,
            "5Min": 5,
            "15Min": 15,
            "30Min": 30,
            "1H": 60,
        }
        return mapping.get(timeframe, 5)

    @staticmethod
    def is_cache_fresh(payload: dict[str, Any], max_age_minutes: float = CACHE_FRESH_MINUTES) -> bool:
        age = payload.get("cache_age_minutes")
        return age is not None and float(age) <= max_age_minutes and int(payload.get("count") or 0) > 0

    def aggregate_bars(self, rows: list[Bar1m], timeframe: str, limit: int) -> list[dict[str, Any]]:
        if not rows:
            return []
        if timeframe == "1Min":
            sliced = rows[-limit:]
            return [
                {
                    "t": (self._ensure_utc(row.ts) or utc_now()).isoformat().replace("+00:00", "Z"),
                    "o": float(row.open),
                    "h": float(row.high),
                    "l": float(row.low),
                    "c": float(row.close),
                    "v": float(row.volume),
                    "source": row.source,
                }
                for row in sliced
            ]

        buckets: dict[datetime, dict[str, Any]] = {}
        for row in rows:
            bucket_ts = self.bucket_bar_ts(row.ts, timeframe)
            bucket = buckets.get(bucket_ts)
            if bucket is None:
                buckets[bucket_ts] = {
                    "bucket_ts": bucket_ts,
                    "o": float(row.open),
                    "h": float(row.high),
                    "l": float(row.low),
                    "c": float(row.close),
                    "v": float(row.volume),
                    "sources": Counter([row.source]),
                }
            else:
                bucket["h"] = max(bucket["h"], float(row.high))
                bucket["l"] = min(bucket["l"], float(row.low))
                bucket["c"] = float(row.close)
                bucket["v"] += float(row.volume)
                bucket["sources"].update([row.source])

        aggregated = []
        for item in sorted(buckets.values(), key=lambda x: x["bucket_ts"])[-limit:]:
            source_counts = item.pop("sources")
            aggregated.append(
                {
                    "t": item["bucket_ts"].isoformat().replace("+00:00", "Z"),
                    "o": item["o"],
                    "h": item["h"],
                    "l": item["l"],
                    "c": item["c"],
                    "v": item["v"],
                    "source": source_counts.most_common(1)[0][0] if source_counts else "bars_1m",
                }
            )
        return aggregated

    def bucket_bar_ts(self, ts: datetime, timeframe: str) -> datetime:
        ts = self._ensure_utc(ts) or utc_now()
        if timeframe == "1Min":
            return ts.replace(second=0, microsecond=0)
        minutes = self.timeframe_to_minutes(timeframe)
        if minutes < 60:
            minute = (ts.minute // minutes) * minutes
            return ts.replace(minute=minute, second=0, microsecond=0)
        hours = max(1, minutes // 60)
        hour = (ts.hour // hours) * hours
        return ts.replace(hour=hour, minute=0, second=0, microsecond=0)

    def _ensure_utc(self, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _is_regular_hours(self, ts: datetime) -> bool:
        ts = self._ensure_utc(ts) or utc_now()
        et = ts.astimezone(_NY)
        current_time = et.time()
        from datetime import time as dt_time
        return dt_time(9, 30) <= current_time < dt_time(16, 0)
