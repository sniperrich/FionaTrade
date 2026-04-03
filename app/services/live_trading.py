"""Live trading service — connects the agent graph to the Alpaca broker.

This service runs a recurring cycle (default: every 5 minutes during US
market hours) that:
  1. Refreshes bar data for active tickers via MarketDataService
  2. Ingests fresh news
  3. Runs the AgentGraph for each configured ticker
  4. Computes position sizing (target_pct × portfolio_value / price)
  5. Places bracket orders via AlpacaBroker (entry + stop-loss + take-profit)
  6. Records every decision in the LiveTrade table

The cycle respects US market hours — no trades are placed outside the
regular session unless live_allow_premarket is set.

On weekends/after-hours: agents still run in "analysis mode" (dry_run=True)
to prepare reasoning for Monday's open, but no orders are placed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
import threading
from typing import Any

import sqlalchemy as sa
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import EXCLUDED_FROM_TRADING, is_follow_up_commentary, normalize_source_name
from app.agent_graph.graph import AgentGraph
from app.broker.alpaca import AlpacaBroker
from app.core.config import DEFAULT_LIVE_ALLOWED_SOURCES, Settings
from app.core.logging import get_app_logger, log_live_cycle
from app.core.market_hours import market_session_info
from app.db.database import db_session
from app.db.models import AgentRun, Bar1m, EntryPlan, Event, EventEvidence, LiveTrade, RawItem, WorkerRun
from app.ingestion.service import IngestionService
from app.services.capital_confirmation import CapitalConfirmationService
from app.services.market_data import MarketDataService
from app.services.runtime_control import CONTROL_LIVE_ENABLED, RuntimeControlService
from app.services.worker_runtime import WorkerRuntimeService
from app.tools.news import count_new_raw_items

logger = get_app_logger()

_MIN_SHARES = 1
_STOP_LOSS_PCT = 0.05
_TAKE_PROFIT_RATIO = 2.0
_LIVE_CYCLE_MUTEX = threading.Lock()
_WAIT_MODES = {"WAIT_PULLBACK", "WAIT_BREAKOUT_CONFIRMATION", "WAIT_UNTIL_OPEN"}
_DIRECTIONAL_ACTIONS = {"BUY", "SHORT", "SELL", "COVER"}


class LiveTradingService:
    """Orchestrates agent decisions → broker orders during market hours."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._agent_graph: AgentGraph | None = None
        self.market_data = MarketDataService(settings)
        self.runtime = WorkerRuntimeService()
        self.capital_confirmation = CapitalConfirmationService()

    def run_cycle(self, session: Session, trigger: str = "scheduled") -> dict[str, Any]:
        if not _LIVE_CYCLE_MUTEX.acquire(blocking=False):
            logger.info("[live] Skipping cycle trigger=%s: another live cycle is already running", trigger)
            return {
                "skipped": True,
                "reason": "live_cycle_in_progress",
                "trigger": trigger,
            }

        cycle_id = str(uuid.uuid4())[:8]
        run: WorkerRun | None = None
        try:
            msi = market_session_info()
            tickers = self._get_tickers()
            tradeable = msi["tradeable"] or (
                msi["label"] == "pre_market" and self.settings.live_allow_premarket
            )
            dry_run = not tradeable

            run = self.runtime.start_run(
                session,
                run_type="live_cycle",
                trigger=trigger,
                run_key=cycle_id,
                stage="starting",
                status="RUNNING",
                market_session=msi["label"],
                dry_run=dry_run,
                total_tickers=len(tickers),
                completed_tickers=0,
            )
            session.commit()
            self._emit_event(
                session,
                run,
                f"Cycle {cycle_id} started",
                stage="starting",
                payload={"market_session": msi["label"], "total_tickers": len(tickers)},
            )
            logger.info("[live] Cycle %s started — %s", cycle_id, msi["context_string"])

            if dry_run:
                self._update_run(session, run, stage="analysis_only", current_agent=None)
                self._emit_event(
                    session,
                    run,
                    f"Cycle {cycle_id} running in analysis mode ({msi['label']})",
                    stage="analysis_only",
                    payload={"market_session": msi["label"]},
                )
                logger.info(
                    "[live] Cycle %s market is %s — running in ANALYSIS mode (no orders)",
                    cycle_id,
                    msi["label"],
                )

            if not tickers:
                summary = {"cycle_id": cycle_id, "error": "No tickers configured for live trading"}
                self._finish_run_record(
                    run_id=run.id,
                    status="COMPLETED",
                    stage="no_tickers",
                    summary=summary,
                    error_message=None,
                    current_ticker=None,
                    current_agent=None,
                    total_tickers=0,
                    completed_tickers=0,
                )
                self._emit_event(
                    session,
                    run,
                    f"Cycle {cycle_id} skipped: no live tickers configured",
                    level="warn",
                    stage="no_tickers",
                    payload={"reason": "no_tickers_configured"},
                )
                return summary

            if not dry_run:
                try:
                    self._update_run(session, run, stage="bar_refresh", current_agent=None)
                    refresh_result = self._refresh_bars(session, tickers)
                    if refresh_result.get("skipped"):
                        self._emit_event(
                            session,
                            run,
                            f"Cycle {cycle_id} bar refresh skipped: {refresh_result.get('reason')}",
                            level="warn",
                            stage="bar_refresh",
                            payload=refresh_result,
                        )
                except Exception as exc:
                    self._emit_event(session, run, f"Cycle {cycle_id} bar refresh failed: {exc}", level="warn", stage="bar_refresh")
                    logger.warning("[live] Bar refresh failed (continuing): %s", exc)

            cycle_start = datetime.now(timezone.utc)
            new_article_count = 0
            new_tradeable_count = 0
            new_tradeable_by_ticker = {t.upper(): 0 for t in tickers}
            allowed_sources = self._allowed_source_set()
            event_driven_mode = bool(getattr(self.settings, "live_event_driven_mode", True))
            run_mode = "full_graph"
            fallback_seconds = self._effective_fallback_cycle_seconds(msi.get("label", "closed"))
            try:
                self._update_run(session, run, stage="ingestion", current_agent=None)
                ingestion = IngestionService(self.settings)
                ingestion.run(
                    session,
                    profile="live_fast",
                    tickers=tickers,
                )
                new_article_count = count_new_raw_items(session, cycle_start)
                new_tradeable_count = self._count_new_tradeable_articles(
                    session,
                    since=cycle_start,
                    tickers=tickers,
                    allowed_sources=allowed_sources,
                )
                new_tradeable_by_ticker = self._count_new_tradeable_by_ticker(
                    session,
                    since=cycle_start,
                    tickers=tickers,
                    allowed_sources=allowed_sources,
                )
            except Exception as exc:
                self._emit_event(session, run, f"Cycle {cycle_id} ingestion failed: {exc}", level="warn", stage="ingestion")
                logger.warning("[live] Ingestion failed (continuing): %s", exc)

            warmup_state = self._warmup_state(session)
            if not dry_run and warmup_state["active"]:
                enabled_at = warmup_state["enabled_at"]
                warmup_until = warmup_state["warmup_until"]
                summary = {
                    "cycle_id": cycle_id,
                    "skipped": True,
                    "reason": "live_warmup",
                    "new_articles": int(new_article_count),
                    "new_tradeable_articles": int(new_tradeable_count),
                    "new_tradeable_articles_by_ticker": new_tradeable_by_ticker,
                    "market_time": msi["et_time_str"],
                    "market_session": msi["label"],
                    "run_key": run.run_key,
                    "event_driven_mode": event_driven_mode,
                    "warmup_active": True,
                    "warmup_remaining_seconds": int(warmup_state["remaining_seconds"]),
                    "live_enabled_at": enabled_at.isoformat() if enabled_at else None,
                    "warmup_until": warmup_until.isoformat() if warmup_until else None,
                    "live_allowed_sources": sorted(allowed_sources),
                }
                self._finish_run_record(
                    run_id=run.id,
                    status="COMPLETED",
                    stage="warmup",
                    summary=summary,
                    current_ticker=None,
                    current_agent=None,
                )
                self._emit_event(
                    session,
                    run,
                    f"Cycle {cycle_id} warm-up active: skipping trades for {warmup_state['remaining_seconds']}s",
                    stage="warmup",
                    payload=summary,
                )
                logger.info(
                    "[live] Cycle %s warm-up active; skipping trading until %s",
                    cycle_id,
                    warmup_until.isoformat() if warmup_until else "unknown",
                )
                return summary

            if event_driven_mode:
                last_global_run = self._get_last_agent_run_time(session, ticker=None)
                time_since_last = (
                    (cycle_start - last_global_run).total_seconds() / 60 if last_global_run else 999
                )
                fallback_minutes = max(1.0, float(fallback_seconds) / 60.0)
                if new_tradeable_count == 0 and time_since_last < fallback_minutes:
                    summary = {
                        "cycle_id": cycle_id,
                        "skipped": True,
                        "reason": "no_new_tradeable_event",
                        "new_articles": int(new_article_count),
                        "new_tradeable_articles": int(new_tradeable_count),
                        "market_time": msi["et_time_str"],
                        "run_key": run.run_key,
                        "event_driven_mode": True,
                        "fallback_cycle_seconds": int(fallback_seconds),
                        "market_session": msi.get("label"),
                        "live_allowed_sources": sorted(allowed_sources),
                    }
                    self._finish_run_record(
                        run_id=run.id,
                        status="COMPLETED",
                        stage="skipped_no_tradeable_event",
                        summary=summary,
                        current_ticker=None,
                        current_agent=None,
                    )
                    self._emit_event(
                        session,
                        run,
                        f"Cycle {cycle_id} skipped: no new tradeable event",
                        stage="skipped_no_tradeable_event",
                        payload={
                            "reason": "no_new_tradeable_event",
                            "fallback_cycle_seconds": int(fallback_seconds),
                            "market_session": msi.get("label"),
                            "live_allowed_sources": sorted(allowed_sources),
                        },
                    )
                    logger.info(
                        "[live] Cycle %s: no new tradeable event (last run %.0f min ago, fallback %.0f min, market=%s) — skipping agents",
                        cycle_id,
                        time_since_last,
                        fallback_minutes,
                        msi.get("label"),
                    )
                    return summary
                if new_tradeable_count == 0:
                    run_mode = "fast_path"
                    self._emit_event(
                        session,
                        run,
                        f"Cycle {cycle_id} fallback tick: no new tradeable event, running fast-path",
                        stage="fallback_fast_path",
                        payload={"reason": "fallback_tick_no_tradeable_event"},
                    )
                else:
                    run_mode = "full_graph"

            broker = AlpacaBroker(self.settings)
            portfolio_value = 100_000.0
            try:
                self._update_run(session, run, stage="broker_state", current_agent=None)
                portfolio_value = broker.get_portfolio_value()
            except Exception as exc:
                if not dry_run:
                    summary = {"cycle_id": cycle_id, "error": f"Broker error: {exc}", "run_key": run.run_key}
                    self._finish_run_record(
                        run_id=run.id,
                        status="ERROR",
                        stage="broker_error",
                        error_message=f"Broker error: {exc}",
                        summary=summary,
                    )
                    logger.error("[live] Cannot fetch portfolio value: %s", exc)
                    return summary
                logger.warning("[live] Broker unavailable in analysis mode: %s", exc)

            plan_results: list[dict[str, Any]] = []
            triggered_plan_tickers: set[str] = set()
            if not dry_run and self.settings.live_entry_planning_enabled:
                self._update_run(session, run, stage="entry_plans", current_agent=None)
                self._expire_outdated_entry_plans(session, tickers, run=run)
                plan_eval = self._execute_active_entry_plans(
                    session=session,
                    broker=broker,
                    portfolio_value=portfolio_value,
                    tickers=tickers,
                    cycle_id=cycle_id,
                    msi=msi,
                    run=run,
                )
                plan_results = plan_eval["results"]
                triggered_plan_tickers = set(plan_eval["triggered_tickers"])

            results = []
            ticker_cooldown_skipped = 0
            for idx, ticker in enumerate(tickers, start=1):
                try:
                    if ticker in triggered_plan_tickers:
                        results.append(
                            {
                                "ticker": ticker,
                                "action": "HOLD",
                                "order_placed": False,
                                "reason": "entry_plan_triggered_this_cycle",
                            }
                        )
                        continue
                    if event_driven_mode:
                        should_skip, minutes_since_last, cooldown_minutes = self._should_skip_ticker_by_cooldown(
                            session,
                            ticker=ticker,
                            cycle_start=cycle_start,
                            new_tradeable_by_ticker=new_tradeable_by_ticker,
                        )
                        if should_skip:
                            ticker_cooldown_skipped += 1
                            results.append(
                                {
                                    "ticker": ticker,
                                    "action": "HOLD",
                                    "order_placed": False,
                                    "skipped": True,
                                    "reason": "ticker_cooldown_no_new_event",
                                    "minutes_since_last": round(float(minutes_since_last or 0.0), 2),
                                    "cooldown_minutes": int(cooldown_minutes),
                                    "new_tradeable_articles": int(new_tradeable_by_ticker.get(ticker.upper(), 0)),
                                }
                            )
                            self._emit_event(
                                session,
                                run,
                                f"{ticker} skipped: no new event + cooldown {cooldown_minutes}m",
                                stage="ticker_skipped_cooldown",
                                ticker=ticker,
                                payload={
                                    "reason": "ticker_cooldown_no_new_event",
                                    "minutes_since_last": round(float(minutes_since_last or 0.0), 2),
                                    "cooldown_minutes": int(cooldown_minutes),
                                    "new_tradeable_articles": int(new_tradeable_by_ticker.get(ticker.upper(), 0)),
                                },
                            )
                            continue
                    self._update_run(
                        session,
                        run,
                        stage="processing_ticker",
                        current_ticker=ticker,
                        current_agent="agent_graph",
                        completed_tickers=idx - 1,
                        total_tickers=len(tickers),
                    )
                    result = self._process_ticker(
                        session,
                        broker,
                        ticker,
                        portfolio_value,
                        cycle_id,
                        msi,
                        dry_run=dry_run,
                        run=run,
                        fast_path=(run_mode == "fast_path"),
                        allowed_sources=allowed_sources,
                    )
                    results.append(result)
                    session.commit()
                    self._emit_event(
                        session,
                        run,
                        f"{ticker} -> {result.get('action', 'HOLD')}",
                        stage="ticker_completed",
                        ticker=ticker,
                        payload={
                            "status": "ok" if not result.get("error") else "error",
                            "action": result.get("action"),
                            "order_placed": bool(result.get("order_placed")),
                        },
                    )
                except Exception as exc:
                    session.rollback()
                    logger.exception("[live] Error processing %s: %s", ticker, exc)
                    results.append({"ticker": ticker, "error": str(exc)})
                    self._emit_event(
                        session,
                        run,
                        f"{ticker} processing failed: {exc}",
                        level="error",
                        stage="ticker_failed",
                        ticker=ticker,
                    )
                finally:
                    self._update_run(session, run, completed_tickers=idx, total_tickers=len(tickers))

            summary = {
                "cycle_id": cycle_id,
                "market_time": msi["et_time_str"],
                "market_session": msi["label"],
                "dry_run": dry_run,
                "portfolio_value": portfolio_value,
                "tickers_processed": len(tickers),
                "new_articles": new_article_count,
                "new_tradeable_articles": new_tradeable_count,
                "new_tradeable_articles_by_ticker": new_tradeable_by_ticker,
                "live_min_confidence": self._effective_live_min_confidence(),
                "event_driven_mode": event_driven_mode,
                "run_mode": run_mode,
                "fallback_cycle_seconds": int(fallback_seconds),
                "ticker_cooldown_minutes": int(getattr(self.settings, "live_ticker_cooldown_minutes", 60)),
                "ticker_cooldown_skipped": int(ticker_cooldown_skipped),
                "live_allowed_sources": sorted(allowed_sources),
                "orders_placed": sum(1 for r in results if r.get("order_placed")) + sum(1 for r in plan_results if r.get("order_placed")),
                "plans_triggered": sum(1 for r in plan_results if r.get("order_placed")),
                "plans_evaluated": len(plan_results),
                "results": results,
                "plan_results": plan_results,
                "run_key": run.run_key,
            }
            logger.info(
                "[live] Cycle %s done — %d/%d orders placed%s",
                cycle_id,
                summary["orders_placed"],
                len(tickers),
                " (ANALYSIS MODE)" if dry_run else "",
            )
            session.commit()
            self._finish_run_record(
                run_id=run.id,
                status="COMPLETED",
                stage="completed",
                summary=summary,
                error_message=None,
                current_ticker=None,
                current_agent=None,
                completed_tickers=len(tickers),
                total_tickers=len(tickers),
            )
            self._emit_event(
                session,
                run,
                f"Cycle {cycle_id} completed: {summary['orders_placed']} orders",
                stage="completed",
                payload={"orders_placed": summary["orders_placed"], "dry_run": dry_run},
            )
            try:
                log_live_cycle(cycle_id, summary)
            except Exception:
                pass
            return summary
        except Exception as exc:
            session.rollback()
            if run is not None:
                failure_summary = {"cycle_id": cycle_id, "error": str(exc), "run_key": run.run_key}
                self._finish_run_record(
                    run_id=run.id,
                    status="FAILED",
                    stage="failed",
                    summary=failure_summary,
                    error_message=str(exc),
                    current_ticker=None,
                    current_agent=None,
                )
                self._persist_runtime_event(
                    run_id=run.id,
                    message=f"Cycle {cycle_id} failed: {exc}",
                    level="error",
                    stage="failed",
                    payload=failure_summary,
                )
            raise
        finally:
            _LIVE_CYCLE_MUTEX.release()

    def _update_run(self, session: Session, run: WorkerRun, **fields: Any) -> None:
        self._persist_run_update(run_id=run.id, **fields)

    def _emit_event(
        self,
        session: Session,
        run: WorkerRun,
        message: str,
        *,
        level: str = "info",
        stage: str | None = None,
        ticker: str | None = None,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._persist_runtime_event(
            run_id=run.id,
            message=message,
            level=level,
            stage=stage,
            ticker=ticker,
            agent=agent,
            payload=payload,
        )

    def _emit_plan_event(
        self,
        session: Session,
        *,
        run: WorkerRun | None,
        stage: str,
        ticker: str | None,
        message: str,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        if run is None:
            return
        normalized_payload = dict(payload or {})
        normalized_payload.setdefault("event", stage)
        self._persist_runtime_event(
            run_id=run.id,
            message=message,
            level=level,
            stage=stage,
            ticker=ticker,
            agent="entry_planner",
            payload=normalized_payload,
        )

    def _persist_run_update(self, *, run_id: int | None, **fields: Any) -> None:
        if not run_id:
            return
        try:
            with db_session() as runtime_session:
                db_run = runtime_session.get(WorkerRun, run_id)
                if db_run is None:
                    return
                self.runtime.update_run(runtime_session, db_run, **fields)
        except Exception as exc:
            logger.warning(
                "[live] runtime run update failed for run_id=%s stage=%s: %s",
                run_id,
                fields.get("stage"),
                exc,
            )

    def _finish_run_record(
        self,
        *,
        run_id: int | None,
        status: str,
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
        **fields: Any,
    ) -> None:
        if not run_id:
            return
        try:
            with db_session() as runtime_session:
                db_run = runtime_session.get(WorkerRun, run_id)
                if db_run is None:
                    return
                self.runtime.finish_run(
                    runtime_session,
                    db_run,
                    status=status,
                    summary=summary,
                    error_message=error_message,
                    **fields,
                )
        except Exception as exc:
            logger.warning(
                "[live] runtime finish_run failed for run_id=%s status=%s: %s",
                run_id,
                status,
                exc,
            )

    def _persist_runtime_event(
        self,
        *,
        run_id: int | None,
        message: str,
        level: str = "info",
        stage: str | None = None,
        ticker: str | None = None,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not run_id:
            return
        try:
            with db_session() as runtime_session:
                db_run = runtime_session.get(WorkerRun, run_id)
                if db_run is None:
                    return
                self.runtime.add_event(
                    runtime_session,
                    "live_cycle",
                    message,
                    run=db_run,
                    level=level,
                    stage=stage,
                    ticker=ticker,
                    agent=agent,
                    payload=payload,
                )
        except Exception as exc:
            logger.warning(
                "[live] runtime event write failed for run_id=%s stage=%s ticker=%s: %s",
                run_id,
                stage,
                ticker,
                exc,
            )

    def _get_tickers(self) -> list[str]:
        tickers = list(self.settings.live_trading_tickers)
        if not tickers:
            tickers = list(self.settings.agent_tickers_override or [])
        return [t.upper() for t in tickers if t]

    def _persist_progress_update(
        self,
        *,
        run_id: int | None,
        ticker: str,
        cycle_id: str,
        update: dict[str, Any],
    ) -> None:
        if not run_id:
            return
        self._persist_run_update(
            run_id=run_id,
            stage=update.get("stage", "agent_graph"),
            current_ticker=ticker,
            current_agent=update.get("agent"),
        )
        if update.get("message"):
            self._persist_runtime_event(
                run_id=run_id,
                message=str(update["message"]),
                ticker=ticker,
                agent=update.get("agent"),
                stage=update.get("stage"),
                payload={"cycle_id": cycle_id},
            )

    def _allowed_source_set(self) -> set[str]:
        configured = getattr(self.settings, "live_allowed_sources", []) or []
        normalized = {
            normalize_source_name(str(source).strip().lower())
            for source in configured
            if str(source).strip()
        }
        if normalized:
            return normalized
        return {
            normalize_source_name(source)
            for source in DEFAULT_LIVE_ALLOWED_SOURCES
        }

    @staticmethod
    def _parse_utc_dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if not value or not isinstance(value, str):
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def _live_control_state(self, session: Session) -> dict[str, Any]:
        return RuntimeControlService().get(session, CONTROL_LIVE_ENABLED) or {}

    def _effective_news_since(
        self,
        *,
        last_run_at: datetime | None,
        live_enabled_at: datetime | None,
    ) -> datetime | None:
        candidates = [dt for dt in (last_run_at, live_enabled_at) if dt is not None]
        if not candidates:
            return None
        return max(candidates)

    def _warmup_state(self, session: Session) -> dict[str, Any]:
        control = self._live_control_state(session)
        enabled_at = self._parse_utc_dt(control.get("enabled_at"))
        warmup_until = self._parse_utc_dt(control.get("warmup_until"))
        now = datetime.now(timezone.utc)
        active = bool(
            control.get("enabled", False)
            and warmup_until is not None
            and warmup_until > now
        )
        remaining = max(0, int((warmup_until - now).total_seconds())) if active and warmup_until else 0
        return {
            "enabled_at": enabled_at,
            "warmup_until": warmup_until,
            "active": active,
            "remaining_seconds": remaining,
        }

    def _find_trigger_event(
        self,
        session: Session,
        *,
        ticker: str,
        since: datetime | None,
        allowed_sources: set[str],
    ) -> dict[str, Any] | None:
        stmt = (
            select(Event)
            .where(
                Event.validation_status == "VALID",
                Event.confidence >= max(0, self._effective_live_min_confidence()),
                Event.event_type.notin_(tuple(EXCLUDED_FROM_TRADING)),
                Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%'),
            )
            .order_by(desc(Event.event_time), desc(Event.created_at), desc(Event.id))
            .limit(20)
        )
        if since is not None:
            stmt = stmt.where(sa.or_(Event.event_time >= since, Event.created_at >= since))

        events = session.execute(stmt).scalars().all()
        for event in events:
            evidences = session.execute(
                select(EventEvidence)
                .where(EventEvidence.event_id == event.id)
                .order_by(EventEvidence.source_tier.asc(), EventEvidence.captured_at.asc(), EventEvidence.id.asc())
            ).scalars().all()
            payload = self._event_payload(event, evidences, allowed_sources=allowed_sources)
            if payload is not None:
                return payload
        return None

    def _event_payload(
        self,
        event: Event,
        evidences: list[EventEvidence],
        *,
        allowed_sources: set[str] | None = None,
    ) -> dict[str, Any] | None:
        if not evidences:
            return None
        normalized_sources = {
            normalize_source_name(ev.source)
            for ev in evidences
            if getattr(ev, "source", None)
        }
        if allowed_sources and not normalized_sources.intersection(allowed_sources):
            return None
        high_quality_sources = {
            normalize_source_name(ev.source)
            for ev in evidences
            if getattr(ev, "source_tier", 9) <= 1 and getattr(ev, "source", None)
        }
        return {
            "id": int(event.id),
            "event_type": str(event.event_type or "unknown"),
            "confidence": int(event.confidence or 0),
            "summary": str(event.summary or ""),
            "high_quality_source_count": len(high_quality_sources),
            "source_count": len(normalized_sources),
            "sources": sorted(normalized_sources),
        }

    def _trigger_event_for_agent_run(
        self,
        session: Session,
        *,
        agent_run_id: int | None,
    ) -> dict[str, Any] | None:
        if not agent_run_id:
            return None
        row = session.get(AgentRun, int(agent_run_id))
        if row is None or row.trigger_event_id is None:
            return None
        event = session.get(Event, int(row.trigger_event_id))
        if event is None:
            return None
        evidences = session.execute(
            select(EventEvidence)
            .where(EventEvidence.event_id == event.id)
            .order_by(EventEvidence.source_tier.asc(), EventEvidence.captured_at.asc(), EventEvidence.id.asc())
        ).scalars().all()
        return self._event_payload(event, evidences, allowed_sources=None)

    def _count_startup_new_positions(
        self,
        session: Session,
        *,
        enabled_at: datetime | None,
    ) -> int:
        if enabled_at is None:
            return 0
        return int(
            session.execute(
                select(sa.func.count())
                .select_from(LiveTrade)
                .where(
                    LiveTrade.created_at >= enabled_at,
                    LiveTrade.status.in_(["submitted", "filled", "pending_new"]),
                    LiveTrade.action.in_(["BUY", "SHORT"]),
                    LiveTrade.quantity > 0,
                )
            ).scalar_one()
            or 0
        )

    def _portfolio_exposure_state(self, broker: AlpacaBroker) -> dict[str, Any]:
        snapshot = broker.get_account()
        positions = broker.get_all_positions()
        equity = float(snapshot.get("equity") or 0.0)
        long_exposure = sum(max(float(pos.market_value or 0.0), 0.0) for pos in positions)
        short_exposure = sum(abs(min(float(pos.market_value or 0.0), 0.0)) for pos in positions)
        return {
            "equity": equity,
            "positions": positions,
            "long_exposure_pct": (long_exposure / equity) if equity > 0 else 0.0,
            "short_exposure_pct": (short_exposure / equity) if equity > 0 else 0.0,
            "long_positions_count": sum(1 for pos in positions if float(pos.quantity or 0.0) > 0),
            "short_positions_count": sum(1 for pos in positions if float(pos.quantity or 0.0) < 0),
        }

    def _same_theme_direction_count(
        self,
        session: Session,
        *,
        positions: list[Any],
        desired_action: str,
        event_type: str | None,
    ) -> int:
        if not event_type:
            return 0
        tickers = [str(pos.ticker).upper() for pos in positions]
        if not tickers:
            return 0
        desired_side = "LONG" if desired_action == "BUY" else "SHORT"
        latest_rows = session.execute(
            select(LiveTrade.ticker, LiveTrade.agent_run_id)
            .where(
                LiveTrade.ticker.in_(tickers),
                LiveTrade.status.in_(["submitted", "filled", "pending_new"]),
                LiveTrade.agent_run_id.is_not(None),
            )
            .order_by(desc(LiveTrade.id))
        ).all()
        latest_by_ticker: dict[str, int] = {}
        for live_ticker, agent_run_id in latest_rows:
            ticker_key = str(live_ticker).upper()
            if ticker_key not in latest_by_ticker and agent_run_id is not None:
                latest_by_ticker[ticker_key] = int(agent_run_id)
        if not latest_by_ticker:
            return 0

        runs = session.execute(
            select(AgentRun.id, AgentRun.trigger_event_id).where(AgentRun.id.in_(list(latest_by_ticker.values())))
        ).all()
        trigger_map = {int(run_id): trigger_event_id for run_id, trigger_event_id in runs if trigger_event_id is not None}
        if not trigger_map:
            return 0

        events = session.execute(
            select(Event.id, Event.event_type).where(Event.id.in_(list(trigger_map.values())))
        ).all()
        event_map = {int(event_id): str(ev_type or "unknown") for event_id, ev_type in events}

        count = 0
        for pos in positions:
            pos_side = "LONG" if float(pos.quantity or 0.0) > 0 else "SHORT"
            if pos_side != desired_side:
                continue
            agent_run_id = latest_by_ticker.get(str(pos.ticker).upper())
            if agent_run_id is None:
                continue
            trigger_event_id = trigger_map.get(agent_run_id)
            if trigger_event_id is None:
                continue
            if event_map.get(int(trigger_event_id)) == event_type:
                count += 1
        return count

    @staticmethod
    def _source_allowed(source: str | None, allowed_sources: set[str]) -> bool:
        if not allowed_sources:
            return True
        return normalize_source_name(str(source or "").strip().lower()) in allowed_sources

    def _effective_live_min_confidence(self) -> int:
        configured = int(getattr(self.settings, "live_min_confidence", 0) or 0)
        if configured <= 0:
            configured = int(getattr(self.settings, "min_trade_confidence", 0) or 0)
        return max(0, min(100, configured))

    def _effective_fallback_cycle_seconds(self, market_session: str) -> int:
        """Return event-driven fallback interval by market session."""
        legacy = max(60, int(getattr(self.settings, "live_fallback_cycle_seconds", 600) or 600))
        if str(market_session or "").lower() in {"open", "market_open"}:
            open_seconds = int(getattr(self.settings, "live_open_cycle_seconds", legacy) or legacy)
            return max(60, open_seconds)
        closed_default = max(legacy, 7200)
        closed_seconds = int(getattr(self.settings, "live_closed_cycle_seconds", closed_default) or closed_default)
        return max(60, closed_seconds)

    def _should_skip_ticker_by_cooldown(
        self,
        session: Session,
        *,
        ticker: str,
        cycle_start: datetime,
        new_tradeable_by_ticker: dict[str, int],
    ) -> tuple[bool, float | None, int]:
        cooldown_minutes = max(0, int(getattr(self.settings, "live_ticker_cooldown_minutes", 60) or 0))
        if cooldown_minutes <= 0:
            return False, None, cooldown_minutes
        if int(new_tradeable_by_ticker.get(ticker.upper(), 0)) > 0:
            return False, None, cooldown_minutes
        last_ticker_run = self._get_last_agent_run_time(session, ticker=ticker)
        if last_ticker_run is None:
            return False, None, cooldown_minutes
        minutes_since_last = (cycle_start - last_ticker_run).total_seconds() / 60.0
        if minutes_since_last < float(cooldown_minutes):
            return True, minutes_since_last, cooldown_minutes
        return False, minutes_since_last, cooldown_minutes

    @staticmethod
    def _extract_final_confidence(state: dict[str, Any]) -> int:
        raw = None
        portfolio = state.get("portfolio_manager_result")
        if isinstance(portfolio, dict):
            raw = portfolio.get("confidence")
        if raw is None:
            raw = state.get("final_confidence")
        try:
            return max(0, min(100, int(raw)))
        except Exception:
            return 0

    def _latest_cached_close(self, session: Session, ticker: str) -> float | None:
        row = session.execute(
            select(Bar1m.close).where(Bar1m.ticker == ticker).order_by(desc(Bar1m.ts)).limit(1)
        ).first()
        if not row:
            return None
        try:
            return float(row[0])
        except Exception:
            return None

    def _extract_execution_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("execution_plan")
        if not isinstance(plan, dict):
            return {}
        execution_mode = str(plan.get("execution_mode", "") or "").upper().strip()
        planned_action = str(plan.get("planned_action", "") or "").upper().strip()
        valid_for_minutes = int(
            plan.get("valid_for_minutes", getattr(self.settings, "live_entry_plan_default_valid_minutes", 180)) or 180
        )
        valid_for_minutes = max(5, min(valid_for_minutes, 1440))
        planned_position_pct = float(plan.get("planned_position_pct", 0.0) or 0.0)
        planned_position_pct = max(0.0, min(planned_position_pct, float(self.settings.live_max_position_pct)))
        entry_plan = plan.get("entry_plan")
        if not isinstance(entry_plan, dict):
            entry_plan = {}
        if execution_mode not in _WAIT_MODES and execution_mode not in ("IMMEDIATE", "NO_TRADE"):
            execution_mode = "NO_TRADE"
        if planned_action not in ("BUY", "SHORT", "SELL", "HOLD"):
            planned_action = "HOLD"
        return {
            "execution_mode": execution_mode,
            "planned_action": planned_action,
            "planned_position_pct": planned_position_pct,
            "valid_for_minutes": valid_for_minutes,
            "entry_plan": entry_plan,
        }

    def _replace_active_entry_plans(
        self,
        session: Session,
        ticker: str,
        *,
        new_status: str,
        reason: str,
        replaced_by_id: int | None = None,
        exclude_plan_id: int | None = None,
    ) -> int:
        stmt = select(EntryPlan).where(
            EntryPlan.ticker == ticker,
            EntryPlan.status == "ACTIVE",
        )
        if exclude_plan_id is not None:
            stmt = stmt.where(EntryPlan.id != exclude_plan_id)
        rows = session.execute(stmt).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = new_status
            row.trigger_reason = reason[:1000]
            row.replaced_by_id = replaced_by_id
            row.updated_at = now
            if new_status == "CANCELLED":
                row.cancelled_at = now
        if rows:
            session.flush()
        return len(rows)

    def _upsert_entry_plan(
        self,
        session: Session,
        *,
        ticker: str,
        agent_run_id: int | None,
        execution_mode: str,
        planned_action: str,
        target_pct: float,
        entry_plan: dict[str, Any],
        valid_for_minutes: int,
        anchor_price: float | None,
        reason: str,
    ) -> EntryPlan:
        plan = EntryPlan(
            ticker=ticker,
            agent_run_id=agent_run_id,
            status="ACTIVE",
            execution_mode=execution_mode,
            planned_action=planned_action,
            target_pct=target_pct,
            trigger_json=entry_plan,
            anchor_price=anchor_price,
            valid_until=datetime.now(timezone.utc) + timedelta(minutes=valid_for_minutes),
            trigger_reason=reason[:1000],
        )
        session.add(plan)
        session.flush()
        self._replace_active_entry_plans(
            session,
            ticker,
            new_status="REPLACED",
            reason="superseded by newer entry plan",
            replaced_by_id=plan.id,
            exclude_plan_id=plan.id,
        )
        return plan

    def _expire_outdated_entry_plans(
        self,
        session: Session,
        tickers: list[str],
        *,
        run: WorkerRun | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        rows = session.execute(
            select(EntryPlan).where(
                EntryPlan.ticker.in_(tickers),
                EntryPlan.status == "ACTIVE",
                EntryPlan.valid_until.is_not(None),
            )
        ).scalars().all()
        changed = False
        for row in rows:
            valid_until = row.valid_until
            if valid_until is None:
                continue
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if valid_until < now:
                row.status = "EXPIRED"
                row.updated_at = now
                row.trigger_reason = "entry plan expired before trigger"
                changed = True
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_expired",
                    ticker=row.ticker,
                    message=f"Entry plan #{row.id} expired before trigger",
                    level="warn",
                    payload={
                        "plan_id": row.id,
                        "status": "expired",
                        "trigger_reason": row.trigger_reason,
                    },
                )
        if changed:
            session.flush()

    def _price_breakout_range(
        self,
        session: Session,
        ticker: str,
        lookback_min: int,
    ) -> tuple[float | None, float | None]:
        rows = session.execute(
            select(Bar1m.high, Bar1m.low)
            .where(Bar1m.ticker == ticker)
            .order_by(desc(Bar1m.ts))
            .limit(max(3, lookback_min))
        ).all()
        if not rows:
            return None, None
        highs = [float(row[0]) for row in rows]
        lows = [float(row[1]) for row in rows]
        return max(highs), min(lows)

    def _evaluate_entry_plan_trigger(
        self,
        session: Session,
        plan: EntryPlan,
        *,
        current_price: float,
        msi: dict[str, Any],
    ) -> tuple[bool, str]:
        mode = str(plan.execution_mode or "").upper()
        action = str(plan.planned_action or "").upper()
        trigger = dict(plan.trigger_json or {})

        if mode == "WAIT_UNTIL_OPEN":
            if msi.get("label") == "open" and bool(msi.get("tradeable")):
                return True, "regular session is open"
            return False, f"waiting for regular open (current={msi.get('label')})"

        if mode == "WAIT_PULLBACK":
            pullback_pct = float(trigger.get("pullback_pct", self.settings.live_entry_plan_default_pullback_pct) or 0.0)
            pullback_pct = max(0.05, min(pullback_pct, 10.0))
            anchor = float(plan.anchor_price or 0.0)
            if anchor <= 0:
                return False, "missing anchor price"
            if action == "BUY":
                threshold = anchor * (1 - pullback_pct / 100.0)
                if current_price <= threshold:
                    return True, f"price {current_price:.2f} <= pullback threshold {threshold:.2f}"
                return False, f"waiting pullback to <= {threshold:.2f} (now {current_price:.2f})"
            if action in ("SHORT", "SELL"):
                threshold = anchor * (1 + pullback_pct / 100.0)
                if current_price >= threshold:
                    return True, f"price {current_price:.2f} >= short pullback threshold {threshold:.2f}"
                return False, f"waiting bounce to >= {threshold:.2f} (now {current_price:.2f})"
            return False, f"unsupported planned_action={action} for pullback mode"

        if mode == "WAIT_BREAKOUT_CONFIRMATION":
            lookback_min = int(trigger.get("breakout_lookback_min", self.settings.live_entry_plan_breakout_lookback_min) or 15)
            lookback_min = max(5, min(lookback_min, 120))
            range_high, range_low = self._price_breakout_range(session, plan.ticker, lookback_min)
            if range_high is None or range_low is None:
                return False, "missing breakout range bars"
            if action == "BUY":
                if current_price >= range_high:
                    return True, f"price {current_price:.2f} >= breakout high {range_high:.2f}"
                return False, f"waiting breakout above {range_high:.2f} (now {current_price:.2f})"
            if action in ("SHORT", "SELL"):
                if current_price <= range_low:
                    return True, f"price {current_price:.2f} <= breakdown low {range_low:.2f}"
                return False, f"waiting breakdown below {range_low:.2f} (now {current_price:.2f})"
            return False, f"unsupported planned_action={action} for breakout mode"

        return False, f"unsupported execution mode={mode}"

    def _refresh_bars(self, session: Session, tickers: list[str]) -> dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info("[live] Refreshing Bar1m for %d tickers (%s)", len(tickers), today)
        result = self.market_data.refresh_bars(
            session,
            start_date=today,
            end_date=tomorrow,
            tickers=tickers,
            chunk_days=1,
            sleep_seconds=0.1,
            trigger="live_cycle",
        )
        logger.info("[live] Bar refresh complete: %s", result)
        return result

    def _get_last_agent_run_time(self, session: Session, ticker: str | None) -> datetime | None:
        try:
            stmt = select(AgentRun.created_at).order_by(desc(AgentRun.created_at)).limit(1)
            if ticker:
                stmt = stmt.where(AgentRun.ticker == ticker)
            result = session.execute(stmt).scalar_one_or_none()
            if result and result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result
        except Exception:
            return None

    def _get_cached_agent_output(
        self,
        session: Session,
        *,
        ticker: str,
        field: str,
        ttl_minutes: int,
    ) -> tuple[dict[str, Any] | None, bool]:
        ttl_minutes = max(1, int(ttl_minutes))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
        try:
            stmt = (
                select(AgentRun)
                .where(
                    AgentRun.ticker == ticker.upper(),
                    AgentRun.status == "COMPLETED",
                    AgentRun.created_at >= cutoff,
                )
                .order_by(desc(AgentRun.created_at), desc(AgentRun.id))
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None, False
            value = getattr(row, field, None)
            if isinstance(value, dict) and value:
                return dict(value), True
        except Exception:
            return None, False
        return None, False

    def _annotate_agent_run(
        self,
        session: Session,
        *,
        agent_run_id: int | None,
        updates: dict[str, Any],
    ) -> None:
        if not agent_run_id:
            return
        try:
            with session.begin_nested():
                row = session.get(AgentRun, agent_run_id)
                if row is None:
                    return
                portfolio = dict(row.portfolio_output or {})
                metadata = dict(portfolio.get("metadata") or {})
                live_meta = dict(metadata.get("live_runtime") or {})
                live_meta.update({k: v for k, v in updates.items() if v is not None})
                metadata["live_runtime"] = live_meta
                portfolio["metadata"] = metadata
                row.portfolio_output = portfolio
                session.flush()
        except Exception as exc:
            logger.debug("[live] failed to annotate agent run %s: %s", agent_run_id, exc)

    def _count_new_tradeable_articles(
        self,
        session: Session,
        *,
        since: datetime,
        tickers: list[str],
        allowed_sources: set[str] | None = None,
    ) -> int:
        rows = session.execute(
            select(RawItem).where(RawItem.ingested_at >= since).order_by(desc(RawItem.ingested_at))
        ).scalars().all()
        if not rows:
            return 0
        ticker_set = {t.upper() for t in tickers if t}
        allowed = allowed_sources or set()
        count = 0
        for row in rows:
            if not self._source_allowed(getattr(row, "source", None), allowed):
                continue
            title = (row.title or "").strip()
            body = (row.body or "")[:2000]
            combined = f"{title}\n{body}"
            if is_follow_up_commentary(combined):
                continue
            if ticker_set and not self._raw_item_mentions_any_ticker(row, ticker_set):
                continue
            count += 1
        return count

    def _count_new_tradeable_by_ticker(
        self,
        session: Session,
        *,
        since: datetime,
        tickers: list[str],
        allowed_sources: set[str] | None = None,
    ) -> dict[str, int]:
        ticker_set = {t.upper() for t in tickers if t}
        counts: dict[str, int] = {ticker: 0 for ticker in ticker_set}
        if not ticker_set:
            return counts
        allowed = allowed_sources or set()
        rows = session.execute(
            select(RawItem).where(RawItem.ingested_at >= since).order_by(desc(RawItem.ingested_at))
        ).scalars().all()
        if not rows:
            return counts
        for row in rows:
            if not self._source_allowed(getattr(row, "source", None), allowed):
                continue
            title = (row.title or "").strip()
            body = (row.body or "")[:2000]
            combined = f"{title}\n{body}"
            if is_follow_up_commentary(combined):
                continue
            title_upper = (row.title or "").upper()
            body_upper = (row.body or "").upper()
            meta = row.metadata_json or {}
            meta_ticker = str(meta.get("ticker") or "").upper().strip()
            for ticker in ticker_set:
                if meta_ticker and meta_ticker == ticker:
                    counts[ticker] += 1
                    continue
                if ticker in title_upper or ticker in body_upper:
                    counts[ticker] += 1
        return counts

    @staticmethod
    def _raw_item_mentions_any_ticker(raw: RawItem, ticker_set: set[str]) -> bool:
        meta = raw.metadata_json or {}
        meta_ticker = str(meta.get("ticker") or "").upper().strip()
        if meta_ticker and meta_ticker in ticker_set:
            return True
        title = (raw.title or "").upper()
        body = (raw.body or "").upper()
        for ticker in ticker_set:
            if ticker and (ticker in title or ticker in body):
                return True
        return False

    def _get_agent_graph(self) -> AgentGraph:
        if self._agent_graph is None:
            self._agent_graph = AgentGraph(self.settings)
        return self._agent_graph

    def _compute_stop_take(
        self,
        session: Session,
        ticker: str,
        entry_price: float,
        side: str,
    ) -> tuple[float, float]:
        stop_pct = _STOP_LOSS_PCT
        try:
            rows = (
                session.execute(
                    select(Bar1m.high, Bar1m.low, Bar1m.close)
                    .where(Bar1m.ticker == ticker)
                    .order_by(desc(Bar1m.ts))
                    .limit(91)
                )
                .all()
            )
            if len(rows) >= 14:
                trs = []
                for i in range(1, len(rows)):
                    high = float(rows[i - 1].high)
                    low = float(rows[i - 1].low)
                    prev_close = float(rows[i].close)
                    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                    trs.append(tr)
                atr = sum(trs[-14:]) / 14
                raw_pct = (atr * 2.5) / entry_price
                stop_pct = max(0.03, min(0.08, raw_pct))
        except Exception:
            pass

        tp_pct = stop_pct * _TAKE_PROFIT_RATIO
        if side.upper() in ("BUY", "COVER"):
            stop_price = round(entry_price * (1 - stop_pct), 2)
            tp_price = round(entry_price * (1 + tp_pct), 2)
        else:
            stop_price = round(entry_price * (1 + stop_pct), 2)
            tp_price = round(entry_price * (1 - tp_pct), 2)
        return stop_price, tp_price

    def _has_open_order(self, broker: AlpacaBroker, ticker: str) -> bool:
        try:
            open_orders = broker.get_open_orders(ticker)
            return len(open_orders) > 0
        except Exception:
            return False

    def _process_ticker(
        self,
        session: Session,
        broker: AlpacaBroker,
        ticker: str,
        portfolio_value: float,
        cycle_id: str,
        msi: dict,
        dry_run: bool = False,
        run: WorkerRun | None = None,
        fast_path: bool = False,
        allowed_sources: set[str] | None = None,
    ) -> dict[str, Any]:
        graph = self._get_agent_graph()

        def progress_callback(update: dict[str, Any]) -> None:
            if run is None:
                return
            self._persist_progress_update(
                run_id=run.id,
                ticker=ticker,
                cycle_id=cycle_id,
                update=update,
            )

        last_run_at = self._get_last_agent_run_time(session, ticker=ticker)
        warmup_state = self._warmup_state(session)
        news_since = self._effective_news_since(
            last_run_at=last_run_at,
            live_enabled_at=warmup_state.get("enabled_at"),
        )
        trigger_event = self._find_trigger_event(
            session,
            ticker=ticker,
            since=news_since,
            allowed_sources=allowed_sources or set(),
        )
        graph_context: dict[str, Any] = {"last_agent_run_at": last_run_at} if last_run_at else {}
        if news_since is not None:
            graph_context["news_since"] = news_since
        if trigger_event:
            graph_context["trigger"] = "event"
            graph_context["trigger_event_id"] = int(trigger_event["id"])
            graph_context["trigger_event"] = trigger_event
        if allowed_sources:
            graph_context["allowed_sources"] = sorted(allowed_sources)
        if fast_path:
            macro_cache, used_cached_macro = self._get_cached_agent_output(
                session,
                ticker=ticker,
                field="macro_output",
                ttl_minutes=int(getattr(self.settings, "live_fast_path_macro_ttl_min", 60)),
            )
            fund_cache, used_cached_fund = self._get_cached_agent_output(
                session,
                ticker=ticker,
                field="fundamentals_output",
                ttl_minutes=int(getattr(self.settings, "live_fast_path_fund_ttl_min", 120)),
            )
            graph_context["fast_path_skip_macro_fund"] = True
            graph_context["cached_macro_signal"] = macro_cache
            graph_context["cached_fund_signal"] = fund_cache
            graph_context["used_cached_macro"] = used_cached_macro
            graph_context["used_cached_fundamentals"] = used_cached_fund
        if dry_run:
            graph_context["dry_run"] = True
            graph_context["market_session"] = msi["label"]

        state = graph.run(session, ticker, context=graph_context, progress_callback=progress_callback)
        desired_action = (state.get("final_action") or "HOLD").upper()
        target_pct = float(state.get("final_position_pct") or 0.0)
        reasoning = (state.get("final_reasoning") or "")[:500]
        execution_plan = self._extract_execution_plan(state)
        final_confidence = self._extract_final_confidence(state)
        live_min_confidence = self._effective_live_min_confidence()
        blocked_by_confidence = False
        blocked_by_missing_event = False
        blocked_by_news_conflict = False
        used_cached_macro = bool(state.get("used_cached_macro", graph_context.get("used_cached_macro", False)))
        used_cached_fund = bool(state.get("used_cached_fundamentals", graph_context.get("used_cached_fundamentals", False)))
        if run is not None:
            self._persist_progress_update(
                run_id=run.id,
                ticker=ticker,
                cycle_id=cycle_id,
                update={"stage": "decision_ready", "agent": "portfolio_manager"},
            )

        target_pct = min(target_pct, self.settings.live_max_position_pct)
        if desired_action in {"BUY", "SHORT", "SELL"} and final_confidence < live_min_confidence:
            blocked_by_confidence = True
            prior_action = desired_action
            desired_action = "HOLD"
            target_pct = 0.0
            execution_plan = {}
            reasoning = (
                f"{reasoning} | confidence_gate={final_confidence}<{live_min_confidence}, "
                f"downgraded {prior_action}->HOLD"
            )[:1000]
            if run is not None:
                self._persist_runtime_event(
                    run_id=run.id,
                    message=(
                        f"{ticker}: confidence gate blocked {prior_action} "
                        f"(confidence={final_confidence}, min={live_min_confidence})"
                    ),
                    level="info",
                    stage="confidence_gate_blocked",
                    ticker=ticker,
                    agent="portfolio_manager",
                    payload={
                        "blocked_action": prior_action,
                        "final_confidence": final_confidence,
                        "live_min_confidence": live_min_confidence,
                    },
                )

        news_signal = str((state.get("news_sentiment_result") or {}).get("signal", "") or "").upper().strip()
        if desired_action in {"BUY", "SHORT", "SELL"} and not trigger_event:
            blocked_by_missing_event = True
            prior_action = desired_action
            desired_action = "HOLD"
            target_pct = 0.0
            execution_plan = {}
            reasoning = (
                f"{reasoning} | event_gate=no_trigger_event, downgraded {prior_action}->HOLD"
            )[:1000]
            if run is not None:
                self._persist_runtime_event(
                    run_id=run.id,
                    message=f"{ticker}: event gate blocked {prior_action} (missing trigger_event_id)",
                    level="info",
                    stage="event_gate_blocked",
                    ticker=ticker,
                    agent="portfolio_manager",
                    payload={"blocked_action": prior_action, "reason": "missing_trigger_event"},
                )

        if (
            desired_action in {"SHORT", "SELL"}
            and news_signal == "BUY"
            and trigger_event is not None
            and int(trigger_event.get("high_quality_source_count", 0) or 0) < 2
        ):
            blocked_by_news_conflict = True
            prior_action = desired_action
            desired_action = "HOLD"
            target_pct = 0.0
            execution_plan = {}
            reasoning = (
                f"{reasoning} | news_conflict_gate=BUY_vs_{prior_action}, "
                f"high_quality_sources={trigger_event.get('high_quality_source_count', 0)}<2"
            )[:1000]
            if run is not None:
                self._persist_runtime_event(
                    run_id=run.id,
                    message=f"{ticker}: news conflict blocked {prior_action} (tier0/1 corroboration insufficient)",
                    level="info",
                    stage="news_conflict_blocked",
                    ticker=ticker,
                    agent="portfolio_manager",
                    payload={
                        "blocked_action": prior_action,
                        "news_signal": news_signal,
                        "trigger_event_id": trigger_event.get("id"),
                        "high_quality_source_count": int(trigger_event.get("high_quality_source_count", 0) or 0),
                    },
                )

        agent_run_id: int | None = None
        try:
            latest = session.execute(
                select(AgentRun)
                .where(AgentRun.ticker == ticker)
                .order_by(desc(AgentRun.id))
                .limit(1)
            ).scalar_one_or_none()
            if latest:
                agent_run_id = latest.id
        except Exception:
            pass

        flow_info: dict[str, Any] = {
            "flow_score": None,
            "flow_bucket": None,
            "position_multiplier": None,
        }
        if (
            not dry_run
            and bool(getattr(self.settings, "flow_confirmation_enabled", True))
            and desired_action in {"BUY", "SHORT"}
        ):
            flow = self.capital_confirmation.evaluate(
                session,
                ticker=ticker,
                direction=desired_action,
            )
            multiplier = float(flow.get("position_multiplier", 1.0) or 1.0)
            if bool(getattr(self.settings, "flow_confirmation_soft_gate", True)):
                target_pct = max(0.0, min(self.settings.live_max_position_pct, target_pct * multiplier))

            flow_score = int(flow.get("flow_score", 50) or 50)
            flow_info = {
                "flow_score": flow_score,
                "flow_bucket": flow.get("flow_bucket"),
                "position_multiplier": multiplier,
            }
            reasoning = (
                f"{reasoning} | flow={flow_score} bucket={flow.get('flow_bucket')} x{multiplier:.2f}"
            )[:1000]
            if flow_score < 40:
                planned_action = desired_action
                desired_action = "HOLD"
                execution_plan = {
                    "execution_mode": "WAIT_BREAKOUT_CONFIRMATION",
                    "planned_action": planned_action,
                    "planned_position_pct": target_pct,
                    "valid_for_minutes": int(getattr(self.settings, "live_entry_plan_default_valid_minutes", 180)),
                    "entry_plan": {
                        "breakout_lookback_min": int(getattr(self.settings, "live_entry_plan_breakout_lookback_min", 15)),
                        "notes": "flow_score_below_40",
                    },
                }

        if dry_run:
            status = "analysis" if dry_run else "skipped"
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=0,
                order_id=None,
                status=status,
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning=reasoning,
            )
            self._annotate_agent_run(
                session,
                agent_run_id=agent_run_id,
                updates={
                    "used_cached_macro": used_cached_macro,
                    "used_cached_fundamentals": used_cached_fund,
                    **flow_info,
                    "fast_path": fast_path,
                    "final_confidence": final_confidence,
                    "live_min_confidence": live_min_confidence,
                    "blocked_by_confidence": blocked_by_confidence,
                    "blocked_by_missing_event": blocked_by_missing_event,
                    "blocked_by_news_conflict": blocked_by_news_conflict,
                    "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                },
            )
            return {
                "ticker": ticker,
                "action": desired_action,
                "order_placed": False,
                "dry_run": True,
                "reasoning": reasoning,
                "used_cached_macro": used_cached_macro,
                "used_cached_fundamentals": used_cached_fund,
                "final_confidence": final_confidence,
                "live_min_confidence": live_min_confidence,
                "blocked_by_confidence": blocked_by_confidence,
                "blocked_by_missing_event": blocked_by_missing_event,
                "blocked_by_news_conflict": blocked_by_news_conflict,
                "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                **flow_info,
            }

        if desired_action == "HOLD" and self.settings.live_entry_planning_enabled:
            mode = execution_plan.get("execution_mode", "NO_TRADE")
            planned_action = execution_plan.get("planned_action", "HOLD")
            if mode in _WAIT_MODES and planned_action in ("BUY", "SHORT", "SELL"):
                plan = self._upsert_entry_plan(
                    session,
                    ticker=ticker,
                    agent_run_id=agent_run_id,
                    execution_mode=mode,
                    planned_action=planned_action,
                    target_pct=min(
                        float(execution_plan.get("planned_position_pct", 0.0) or target_pct),
                        self.settings.live_max_position_pct,
                    ),
                    entry_plan=dict(execution_plan.get("entry_plan") or {}),
                    valid_for_minutes=int(execution_plan.get("valid_for_minutes", self.settings.live_entry_plan_default_valid_minutes)),
                    anchor_price=self._latest_cached_close(session, ticker),
                    reason=reasoning,
                )
                self._record_live_trade(
                    session,
                    cycle_id=cycle_id,
                    ticker=ticker,
                    agent_run_id=agent_run_id,
                    action="HOLD",
                    quantity=0,
                    target_pct=0,
                    order_id=None,
                    status="planned",
                    et_time=msi["et_time_str"],
                    market_session=msi["label"],
                    reasoning=f"Entry plan {plan.execution_mode} created for {planned_action}. {reasoning}",
                )
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_created",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} created: {plan.execution_mode} -> {plan.planned_action}",
                    payload={
                        "plan_id": plan.id,
                        "status": "active",
                        "execution_mode": plan.execution_mode,
                        "planned_action": plan.planned_action,
                        "target_pct": float(plan.target_pct or 0.0),
                    },
                )
                self._annotate_agent_run(
                    session,
                    agent_run_id=agent_run_id,
                    updates={
                        "used_cached_macro": used_cached_macro,
                        "used_cached_fundamentals": used_cached_fund,
                        **flow_info,
                        "fast_path": fast_path,
                        "entry_plan_mode": plan.execution_mode,
                        "final_confidence": final_confidence,
                        "live_min_confidence": live_min_confidence,
                        "blocked_by_confidence": blocked_by_confidence,
                        "blocked_by_missing_event": blocked_by_missing_event,
                        "blocked_by_news_conflict": blocked_by_news_conflict,
                        "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                    },
                )
                return {
                    "ticker": ticker,
                    "action": "HOLD",
                    "order_placed": False,
                    "plan_created": True,
                    "plan_id": plan.id,
                    "plan_mode": plan.execution_mode,
                    "planned_action": plan.planned_action,
                    "used_cached_macro": used_cached_macro,
                    "used_cached_fundamentals": used_cached_fund,
                    "final_confidence": final_confidence,
                    "live_min_confidence": live_min_confidence,
                    "blocked_by_confidence": blocked_by_confidence,
                    "blocked_by_missing_event": blocked_by_missing_event,
                    "blocked_by_news_conflict": blocked_by_news_conflict,
                    "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                    **flow_info,
                }

        if desired_action == "HOLD":
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=0,
                order_id=None,
                status="skipped",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning=reasoning,
            )
            self._annotate_agent_run(
                session,
                agent_run_id=agent_run_id,
                updates={
                    "used_cached_macro": used_cached_macro,
                    "used_cached_fundamentals": used_cached_fund,
                    **flow_info,
                    "fast_path": fast_path,
                    "final_confidence": final_confidence,
                    "live_min_confidence": live_min_confidence,
                    "blocked_by_confidence": blocked_by_confidence,
                    "blocked_by_missing_event": blocked_by_missing_event,
                    "blocked_by_news_conflict": blocked_by_news_conflict,
                    "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                },
            )
            return {
                "ticker": ticker,
                "action": "HOLD",
                "order_placed": False,
                "used_cached_macro": used_cached_macro,
                "used_cached_fundamentals": used_cached_fund,
                "final_confidence": final_confidence,
                "live_min_confidence": live_min_confidence,
                "blocked_by_confidence": blocked_by_confidence,
                "blocked_by_missing_event": blocked_by_missing_event,
                "blocked_by_news_conflict": blocked_by_news_conflict,
                "trigger_event_id": trigger_event.get("id") if trigger_event else None,
                **flow_info,
            }

        if self.settings.live_entry_planning_enabled:
            invalidated_count = self._replace_active_entry_plans(
                session,
                ticker,
                new_status="INVALIDATED",
                reason="immediate signal superseded pending entry plan",
            )
            if invalidated_count > 0:
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_invalidated",
                    ticker=ticker,
                    message=f"{invalidated_count} active entry plan(s) invalidated by immediate {desired_action}",
                    level="warn",
                    payload={
                        "status": "invalidated",
                        "count": invalidated_count,
                        "immediate_action": desired_action,
                    },
                )

        order_result = self._submit_order_for_action(
            session=session,
            broker=broker,
            ticker=ticker,
            desired_action=desired_action,
                target_pct=target_pct,
                portfolio_value=portfolio_value,
                cycle_id=cycle_id,
                msi=msi,
                agent_run_id=agent_run_id,
                reasoning=reasoning,
                run=run,
                trigger_event=trigger_event,
            )
        order_result.update(
            {
                "used_cached_macro": used_cached_macro,
                "used_cached_fundamentals": used_cached_fund,
                **flow_info,
                "fast_path": fast_path,
                "final_confidence": final_confidence,
                "live_min_confidence": live_min_confidence,
                "blocked_by_confidence": blocked_by_confidence,
                "blocked_by_missing_event": blocked_by_missing_event,
                "blocked_by_news_conflict": blocked_by_news_conflict,
                "trigger_event_id": trigger_event.get("id") if trigger_event else None,
            }
        )
        self._annotate_agent_run(
            session,
            agent_run_id=agent_run_id,
            updates={
                "used_cached_macro": used_cached_macro,
                "used_cached_fundamentals": used_cached_fund,
                **flow_info,
                "fast_path": fast_path,
                "final_target_pct": target_pct,
                "final_confidence": final_confidence,
                "live_min_confidence": live_min_confidence,
                "blocked_by_confidence": blocked_by_confidence,
                "blocked_by_missing_event": blocked_by_missing_event,
                "blocked_by_news_conflict": blocked_by_news_conflict,
                "trigger_event_id": trigger_event.get("id") if trigger_event else None,
            },
        )
        return order_result

    def _execute_active_entry_plans(
        self,
        *,
        session: Session,
        broker: AlpacaBroker,
        portfolio_value: float,
        tickers: list[str],
        cycle_id: str,
        msi: dict[str, Any],
        run: WorkerRun | None,
    ) -> dict[str, Any]:
        rows = session.execute(
            select(EntryPlan).where(
                EntryPlan.status == "ACTIVE",
                EntryPlan.ticker.in_(tickers),
            ).order_by(EntryPlan.created_at.asc(), EntryPlan.id.asc())
        ).scalars().all()
        results: list[dict[str, Any]] = []
        triggered_tickers: set[str] = set()
        for plan in rows:
            ticker = plan.ticker
            try:
                current_price = broker.get_latest_price(ticker)
            except Exception as exc:
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_evaluated",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} skipped: latest price fetch failed",
                    level="warn",
                    payload={
                        "plan_id": plan.id,
                        "status": "price_fetch_failed",
                        "error": str(exc),
                    },
                )
                results.append(
                    {
                        "plan_id": plan.id,
                        "ticker": ticker,
                        "order_placed": False,
                        "reason": "price_fetch_failed",
                        "error": str(exc),
                    }
                )
                continue
            if not current_price or current_price <= 0:
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_evaluated",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} skipped: latest price unavailable",
                    level="warn",
                    payload={
                        "plan_id": plan.id,
                        "status": "price_unavailable",
                    },
                )
                results.append(
                    {
                        "plan_id": plan.id,
                        "ticker": ticker,
                        "order_placed": False,
                        "reason": "price_unavailable",
                    }
                )
                continue
            should_trigger, trigger_reason = self._evaluate_entry_plan_trigger(
                session,
                plan,
                current_price=float(current_price),
                msi=msi,
            )
            if not should_trigger:
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_evaluated",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} waiting: {trigger_reason}",
                    payload={
                        "plan_id": plan.id,
                        "status": "waiting",
                        "trigger_reason": trigger_reason,
                    },
                )
                results.append(
                    {
                        "plan_id": plan.id,
                        "ticker": ticker,
                        "order_placed": False,
                        "reason": "not_triggered",
                        "trigger_reason": trigger_reason,
                    }
                )
                continue

            exec_result = self._submit_order_for_action(
                session=session,
                broker=broker,
                ticker=ticker,
                desired_action=plan.planned_action,
                target_pct=float(plan.target_pct or 0.0),
                portfolio_value=portfolio_value,
                cycle_id=cycle_id,
                msi=msi,
                agent_run_id=plan.agent_run_id,
                reasoning=f"Triggered entry plan #{plan.id}: {trigger_reason}",
                run=run,
                trigger_event=self._trigger_event_for_agent_run(session, agent_run_id=plan.agent_run_id),
            )
            if exec_result.get("order_placed"):
                plan.status = "TRIGGERED"
                plan.triggered_at = datetime.now(timezone.utc)
                plan.updated_at = datetime.now(timezone.utc)
                plan.trigger_reason = trigger_reason[:1000]
                triggered_tickers.add(ticker)
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_triggered",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} triggered: {trigger_reason}",
                    payload={
                        "plan_id": plan.id,
                        "status": "triggered",
                        "trigger_reason": trigger_reason,
                        "order_id": exec_result.get("order_id"),
                        "action": exec_result.get("action") or plan.planned_action,
                    },
                )
            else:
                # Keep ACTIVE so it can be evaluated again unless explicitly terminal.
                plan.updated_at = datetime.now(timezone.utc)
                plan.trigger_reason = f"trigger matched but order not placed: {exec_result.get('reason') or exec_result.get('error') or 'unknown'}"[:1000]
                self._emit_plan_event(
                    session,
                    run=run,
                    stage="entry_plan_trigger_failed",
                    ticker=ticker,
                    message=f"Entry plan #{plan.id} trigger matched but order failed",
                    level="warn",
                    payload={
                        "plan_id": plan.id,
                        "status": "trigger_failed",
                        "trigger_reason": trigger_reason,
                        "reason": exec_result.get("reason"),
                        "error": exec_result.get("error"),
                    },
                )
            session.flush()
            results.append(
                {
                    "plan_id": plan.id,
                    "ticker": ticker,
                    "order_placed": bool(exec_result.get("order_placed")),
                    "trigger_reason": trigger_reason,
                    "execution_result": exec_result,
                }
            )
        return {
            "results": results,
            "triggered_tickers": sorted(triggered_tickers),
        }

    def _submit_order_for_action(
        self,
        *,
        session: Session,
        broker: AlpacaBroker,
        ticker: str,
        desired_action: str,
        target_pct: float,
        portfolio_value: float,
        cycle_id: str,
        msi: dict[str, Any],
        agent_run_id: int | None,
        reasoning: str,
        run: WorkerRun | None,
        trigger_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if run is not None:
            self._persist_progress_update(
                run_id=run.id,
                ticker=ticker,
                cycle_id=cycle_id,
                update={"stage": "pricing", "agent": None},
            )
        cache_is_fresh, cache_age_minutes = self.market_data.is_ticker_cache_fresh(
            session,
            ticker,
            max_age_minutes=self.settings.live_data_max_age_minutes,
        )
        if not cache_is_fresh:
            freshness_label = f"{cache_age_minutes:.1f}m old" if cache_age_minutes is not None else "missing"
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=target_pct,
                order_id=None,
                status="skipped",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning=f"Local market data is stale ({freshness_label}); order suppressed",
            )
            return {
                "ticker": ticker,
                "action": desired_action,
                "order_placed": False,
                "reason": "stale_market_data",
                "cache_age_minutes": cache_age_minutes,
            }

        current_price = broker.get_latest_price(ticker)
        if not current_price or current_price <= 0:
            logger.warning("[live] No price for %s — skipping", ticker)
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=target_pct,
                order_id=None,
                status="error",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning=reasoning,
                error="Could not fetch current price",
            )
            return {"ticker": ticker, "action": desired_action, "order_placed": False, "error": "No price available"}

        try:
            current_pos = broker.get_position(ticker)
        except Exception:
            current_pos = None
        current_qty = float(current_pos.quantity) if current_pos else 0.0

        if run is not None:
            self._persist_progress_update(
                run_id=run.id,
                ticker=ticker,
                cycle_id=cycle_id,
                update={"stage": "position_sizing", "agent": None},
            )
        target_dollars = portfolio_value * target_pct
        target_qty = int(target_dollars / current_price)
        if target_qty < _MIN_SHARES:
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=target_pct,
                order_id=None,
                status="skipped",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning=f"Insufficient capital: {target_dollars:.0f} USD < 1 share at {current_price:.2f}",
            )
            return {"ticker": ticker, "action": desired_action, "order_placed": False, "reason": "insufficient capital"}

        order_action, order_qty = self._resolve_order(desired_action, target_qty, current_qty)
        if order_action is None or order_qty < _MIN_SHARES:
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=target_pct,
                order_id=None,
                status="no_change",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning="Position already at target",
            )
            return {"ticker": ticker, "action": desired_action, "order_placed": False, "reason": "position unchanged"}

        is_new_directional_position = current_qty == 0 and order_action in {"BUY", "SHORT"}
        live_control = self._live_control_state(session)
        enabled_at = self._parse_utc_dt(live_control.get("enabled_at"))
        ramp_minutes = max(0, int(getattr(self.settings, "live_startup_ramp_minutes", 30) or 0))
        within_startup_ramp = bool(
            enabled_at is not None
            and ramp_minutes > 0
            and datetime.now(timezone.utc) < enabled_at + timedelta(minutes=ramp_minutes)
        )

        if is_new_directional_position and within_startup_ramp:
            startup_limit = max(0, int(getattr(self.settings, "live_startup_max_new_positions", 2) or 0))
            startup_opened = self._count_startup_new_positions(session, enabled_at=enabled_at)
            if startup_limit > 0 and startup_opened >= startup_limit:
                self._record_live_trade(
                    session,
                    cycle_id=cycle_id,
                    ticker=ticker,
                    agent_run_id=agent_run_id,
                    action=desired_action,
                    quantity=0,
                    target_pct=target_pct,
                    order_id=None,
                    status="skipped",
                    et_time=msi["et_time_str"],
                    market_session=msi["label"],
                    reasoning=(
                        f"Startup ramp guard: already opened {startup_opened} new positions "
                        f"within {ramp_minutes}m of enable"
                    ),
                )
                return {
                    "ticker": ticker,
                    "action": desired_action,
                    "order_placed": False,
                    "reason": "startup_ramp_position_cap",
                    "startup_new_positions": startup_opened,
                    "startup_limit": startup_limit,
                }

        if is_new_directional_position:
            exposure = self._portfolio_exposure_state(broker)
            equity = float(exposure.get("equity") or 0.0)
            proposed_notional = float(order_qty) * float(current_price)
            if order_action == "BUY":
                projected_long_exposure_pct = exposure["long_exposure_pct"] + (
                    (proposed_notional / equity) if equity > 0 else 0.0
                )
                max_long = float(getattr(self.settings, "live_max_net_long_exposure_pct", 0.35) or 0.35)
                if projected_long_exposure_pct > max_long:
                    self._record_live_trade(
                        session,
                        cycle_id=cycle_id,
                        ticker=ticker,
                        agent_run_id=agent_run_id,
                        action=desired_action,
                        quantity=0,
                        target_pct=target_pct,
                        order_id=None,
                        status="skipped",
                        et_time=msi["et_time_str"],
                        market_session=msi["label"],
                        reasoning=(
                            f"Net long exposure cap: projected {projected_long_exposure_pct:.1%} "
                            f"> max {max_long:.1%}"
                        ),
                    )
                    return {
                        "ticker": ticker,
                        "action": desired_action,
                        "order_placed": False,
                        "reason": "net_long_exposure_cap",
                        "projected_long_exposure_pct": projected_long_exposure_pct,
                        "max_long_exposure_pct": max_long,
                    }
                max_same_direction = max(0, int(getattr(self.settings, "live_max_same_direction_positions", 4) or 0))
                if max_same_direction > 0 and int(exposure["long_positions_count"]) >= max_same_direction:
                    self._record_live_trade(
                        session,
                        cycle_id=cycle_id,
                        ticker=ticker,
                        agent_run_id=agent_run_id,
                        action=desired_action,
                        quantity=0,
                        target_pct=target_pct,
                        order_id=None,
                        status="skipped",
                        et_time=msi["et_time_str"],
                        market_session=msi["label"],
                        reasoning=(
                            f"Direction concentration cap: already {int(exposure['long_positions_count'])} LONG positions "
                            f"(max {max_same_direction})"
                        ),
                    )
                    return {
                        "ticker": ticker,
                        "action": desired_action,
                        "order_placed": False,
                        "reason": "same_direction_position_cap",
                        "current_direction_positions": int(exposure["long_positions_count"]),
                        "max_same_direction_positions": max_same_direction,
                    }
            elif order_action == "SHORT":
                projected_short_exposure_pct = exposure["short_exposure_pct"] + (
                    (proposed_notional / equity) if equity > 0 else 0.0
                )
                max_short = float(getattr(self.settings, "live_max_net_short_exposure_pct", 0.35) or 0.35)
                if projected_short_exposure_pct > max_short:
                    self._record_live_trade(
                        session,
                        cycle_id=cycle_id,
                        ticker=ticker,
                        agent_run_id=agent_run_id,
                        action=desired_action,
                        quantity=0,
                        target_pct=target_pct,
                        order_id=None,
                        status="skipped",
                        et_time=msi["et_time_str"],
                        market_session=msi["label"],
                        reasoning=(
                            f"Net short exposure cap: projected {projected_short_exposure_pct:.1%} "
                            f"> max {max_short:.1%}"
                        ),
                    )
                    return {
                        "ticker": ticker,
                        "action": desired_action,
                        "order_placed": False,
                        "reason": "net_short_exposure_cap",
                        "projected_short_exposure_pct": projected_short_exposure_pct,
                        "max_short_exposure_pct": max_short,
                    }
                max_same_direction = max(0, int(getattr(self.settings, "live_max_same_direction_positions", 4) or 0))
                if max_same_direction > 0 and int(exposure["short_positions_count"]) >= max_same_direction:
                    self._record_live_trade(
                        session,
                        cycle_id=cycle_id,
                        ticker=ticker,
                        agent_run_id=agent_run_id,
                        action=desired_action,
                        quantity=0,
                        target_pct=target_pct,
                        order_id=None,
                        status="skipped",
                        et_time=msi["et_time_str"],
                        market_session=msi["label"],
                        reasoning=(
                            f"Direction concentration cap: already {int(exposure['short_positions_count'])} SHORT positions "
                            f"(max {max_same_direction})"
                        ),
                    )
                    return {
                        "ticker": ticker,
                        "action": desired_action,
                        "order_placed": False,
                        "reason": "same_direction_position_cap",
                        "current_direction_positions": int(exposure["short_positions_count"]),
                        "max_same_direction_positions": max_same_direction,
                    }

            theme_cap = max(0, int(getattr(self.settings, "live_max_same_theme_direction_positions", 2) or 0))
            if theme_cap > 0 and trigger_event:
                same_theme_count = self._same_theme_direction_count(
                    session,
                    positions=exposure["positions"],
                    desired_action=order_action,
                    event_type=str(trigger_event.get("event_type") or "unknown"),
                )
                if same_theme_count >= theme_cap:
                    self._record_live_trade(
                        session,
                        cycle_id=cycle_id,
                        ticker=ticker,
                        agent_run_id=agent_run_id,
                        action=desired_action,
                        quantity=0,
                        target_pct=target_pct,
                        order_id=None,
                        status="skipped",
                        et_time=msi["et_time_str"],
                        market_session=msi["label"],
                        reasoning=(
                            f"Theme concentration cap: already {same_theme_count} "
                            f"{order_action} positions for event_type={trigger_event.get('event_type')}"
                        ),
                    )
                    return {
                        "ticker": ticker,
                        "action": desired_action,
                        "order_placed": False,
                        "reason": "same_theme_direction_cap",
                        "current_same_theme_positions": same_theme_count,
                        "max_same_theme_positions": theme_cap,
                        "event_type": trigger_event.get("event_type"),
                    }

        if self._has_open_order(broker, ticker):
            logger.info("[live] %s already has an open order — skipping", ticker)
            self._record_live_trade(
                session,
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=agent_run_id,
                action=desired_action,
                quantity=0,
                target_pct=target_pct,
                order_id=None,
                status="skipped",
                et_time=msi["et_time_str"],
                market_session=msi["label"],
                reasoning="Open order already pending for this ticker",
            )
            return {"ticker": ticker, "action": desired_action, "order_placed": False, "reason": "open_order_exists"}

        stop_price, tp_price = self._compute_stop_take(session, ticker, current_price, order_action)
        logger.info(
            "[live] %s %s %d shares @ ~$%.2f | sl=%.2f tp=%.2f (%.1f%% of $%.0f)",
            order_action,
            ticker,
            order_qty,
            current_price,
            stop_price,
            tp_price,
            target_pct * 100,
            portfolio_value,
        )

        if run is not None:
            self._persist_progress_update(
                run_id=run.id,
                ticker=ticker,
                cycle_id=cycle_id,
                update={"stage": "placing_order", "agent": None},
            )
        result = broker.place_bracket_order(
            ticker=ticker,
            action=order_action,
            quantity=order_qty,
            take_profit_price=tp_price,
            stop_loss_price=stop_price,
        )
        status = "submitted" if result.success else "error"
        self._record_live_trade(
            session,
            cycle_id=cycle_id,
            ticker=ticker,
            agent_run_id=agent_run_id,
            action=order_action,
            quantity=order_qty,
            target_pct=target_pct,
            order_id=result.order_id,
            status=status,
            et_time=msi["et_time_str"],
            market_session=msi["label"],
            reasoning=reasoning,
            error=result.error,
        )
        return {
            "ticker": ticker,
            "action": order_action,
            "quantity": order_qty,
            "price": current_price,
            "stop_loss": stop_price,
            "take_profit": tp_price,
            "order_id": result.order_id,
            "order_placed": result.success,
            "error": result.error,
        }

    def _resolve_order(self, desired: str, target_qty: int, current_qty: float) -> tuple[str | None, int]:
        current_long = max(0, current_qty)
        current_short = max(0, -current_qty)

        if desired == "BUY":
            if current_short > 0:
                return "COVER", int(current_short)
            delta = target_qty - int(current_long)
            if delta > 0:
                return "BUY", delta
            return None, 0

        if desired == "SHORT":
            if current_long > 0:
                return "SELL", int(current_long)
            delta = target_qty - int(current_short)
            if delta > 0:
                return "SHORT", delta
            return None, 0

        if desired == "SELL":
            if current_long > 0:
                return "SELL", int(current_long)
            return None, 0

        if desired == "COVER":
            if current_short > 0:
                return "COVER", int(current_short)
            return None, 0

        return None, 0

    def _record_live_trade(
        self,
        session: Session,
        *,
        cycle_id: str,
        ticker: str,
        agent_run_id: int | None,
        action: str,
        quantity: float,
        target_pct: float,
        order_id: str | None,
        status: str,
        et_time: str,
        market_session: str,
        reasoning: str = "",
        error: str | None = None,
    ) -> None:
        try:
            with session.begin_nested():
                trade = LiveTrade(
                    cycle_id=cycle_id,
                    ticker=ticker,
                    agent_run_id=agent_run_id,
                    action=action,
                    quantity=quantity,
                    target_pct=target_pct,
                    order_id=order_id,
                    status=status,
                    et_time=et_time,
                    market_session=market_session,
                    reasoning=reasoning,
                    error=error,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(trade)
                session.flush()
        except Exception as exc:
            logger.warning("[live] Failed to record LiveTrade for %s: %s", ticker, exc)
