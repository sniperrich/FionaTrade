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
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.agent_graph.graph import AgentGraph
from app.broker.alpaca import AlpacaBroker
from app.core.config import Settings
from app.core.logging import get_app_logger, log_live_cycle
from app.core.market_hours import market_session_info
from app.db.models import AgentRun, Bar1m, LiveTrade, WorkerRun
from app.ingestion.service import IngestionService
from app.services.market_data import MarketDataService
from app.services.worker_runtime import WorkerRuntimeService
from app.tools.news import count_new_raw_items

logger = get_app_logger()

_MIN_SHARES = 1
_STOP_LOSS_PCT = 0.05
_TAKE_PROFIT_RATIO = 2.0


class LiveTradingService:
    """Orchestrates agent decisions → broker orders during market hours."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._agent_graph: AgentGraph | None = None
        self.market_data = MarketDataService(settings)
        self.runtime = WorkerRuntimeService()

    def run_cycle(self, session: Session, trigger: str = "scheduled") -> dict[str, Any]:
        cycle_id = str(uuid.uuid4())[:8]
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
            self.runtime.finish_run(
                session,
                run,
                status="COMPLETED",
                stage="no_tickers",
                current_ticker=None,
                current_agent=None,
                total_tickers=0,
                completed_tickers=0,
                summary=summary,
                error_message=None,
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
                self._refresh_bars(session, tickers)
            except Exception as exc:
                self._emit_event(session, run, f"Cycle {cycle_id} bar refresh failed: {exc}", level="warn", stage="bar_refresh")
                logger.warning("[live] Bar refresh failed (continuing): %s", exc)

        cycle_start = datetime.now(timezone.utc)
        new_article_count = 0
        try:
            self._update_run(session, run, stage="ingestion", current_agent=None)
            ingestion = IngestionService(self.settings)
            ingestion.run(
                session,
                profile="live_fast",
                tickers=tickers,
            )
            new_article_count = count_new_raw_items(session, cycle_start)
        except Exception as exc:
            self._emit_event(session, run, f"Cycle {cycle_id} ingestion failed: {exc}", level="warn", stage="ingestion")
            logger.warning("[live] Ingestion failed (continuing): %s", exc)

        if not dry_run:
            last_global_run = self._get_last_agent_run_time(session, ticker=None)
            time_since_last = (
                (cycle_start - last_global_run).total_seconds() / 60 if last_global_run else 999
            )
            if new_article_count == 0 and time_since_last < 30:
                summary = {
                    "cycle_id": cycle_id,
                    "skipped": True,
                    "reason": "no_new_articles",
                    "new_articles": 0,
                    "market_time": msi["et_time_str"],
                    "run_key": run.run_key,
                }
                self.runtime.finish_run(
                    session,
                    run,
                    status="COMPLETED",
                    stage="skipped_no_news",
                    current_ticker=None,
                    current_agent=None,
                    summary=summary,
                )
                self._emit_event(
                    session,
                    run,
                    f"Cycle {cycle_id} skipped: no new articles",
                    stage="skipped_no_news",
                    payload={"reason": "no_new_articles"},
                )
                logger.info(
                    "[live] Cycle %s: no new articles (last run %.0f min ago) — skipping agents",
                    cycle_id,
                    time_since_last,
                )
                return summary

        broker = AlpacaBroker(self.settings)
        portfolio_value = 100_000.0
        try:
            self._update_run(session, run, stage="broker_state", current_agent=None)
            portfolio_value = broker.get_portfolio_value()
        except Exception as exc:
            if not dry_run:
                summary = {"cycle_id": cycle_id, "error": f"Broker error: {exc}", "run_key": run.run_key}
                self.runtime.finish_run(
                    session,
                    run,
                    status="ERROR",
                    stage="broker_error",
                    error_message=f"Broker error: {exc}",
                    summary=summary,
                )
                logger.error("[live] Cannot fetch portfolio value: %s", exc)
                return summary
            logger.warning("[live] Broker unavailable in analysis mode: %s", exc)

        results = []
        for idx, ticker in enumerate(tickers, start=1):
            try:
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
                )
                results.append(result)
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
            "orders_placed": sum(1 for r in results if r.get("order_placed")),
            "results": results,
            "run_key": run.run_key,
        }
        logger.info(
            "[live] Cycle %s done — %d/%d orders placed%s",
            cycle_id,
            summary["orders_placed"],
            len(tickers),
            " (ANALYSIS MODE)" if dry_run else "",
        )
        self.runtime.finish_run(
            session,
            run,
            status="COMPLETED",
            stage="completed",
            current_ticker=None,
            current_agent=None,
            completed_tickers=len(tickers),
            total_tickers=len(tickers),
            summary=summary,
            error_message=None,
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

    def _update_run(self, session: Session, run: WorkerRun, **fields: Any) -> None:
        self.runtime.update_run(session, run, **fields)
        session.commit()

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
        self.runtime.add_event(
            session,
            "live_cycle",
            message,
            run=run,
            level=level,
            stage=stage,
            ticker=ticker,
            agent=agent,
            payload=payload,
        )
        session.commit()

    def _get_tickers(self) -> list[str]:
        tickers = list(self.settings.live_trading_tickers)
        if not tickers:
            tickers = list(self.settings.agent_tickers_override or [])
        return [t.upper() for t in tickers if t]

    def _refresh_bars(self, session: Session, tickers: list[str]) -> None:
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
    ) -> dict[str, Any]:
        graph = self._get_agent_graph()

        def progress_callback(update: dict[str, Any]) -> None:
            if run is None:
                return
            self.runtime.update_run(
                session,
                run,
                stage=update.get("stage", "agent_graph"),
                current_ticker=ticker,
                current_agent=update.get("agent"),
            )
            if update.get("message"):
                self.runtime.add_event(
                    session,
                    "live_cycle",
                    str(update["message"]),
                    run=run,
                    ticker=ticker,
                    agent=update.get("agent"),
                    stage=update.get("stage"),
                    payload={"cycle_id": cycle_id},
                )
            session.commit()

        last_run_at = self._get_last_agent_run_time(session, ticker=ticker)
        graph_context: dict[str, Any] = {"last_agent_run_at": last_run_at} if last_run_at else {}
        if dry_run:
            graph_context["dry_run"] = True
            graph_context["market_session"] = msi["label"]

        state = graph.run(session, ticker, context=graph_context, progress_callback=progress_callback)
        desired_action = (state.get("final_action") or "HOLD").upper()
        target_pct = float(state.get("final_position_pct") or 0.0)
        reasoning = (state.get("final_reasoning") or "")[:500]
        if run is not None:
            self.runtime.update_run(session, run, stage="decision_ready", current_ticker=ticker, current_agent="portfolio_manager")
            session.commit()

        target_pct = min(target_pct, self.settings.live_max_position_pct)

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

        if desired_action == "HOLD" or dry_run:
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
            if dry_run:
                return {"ticker": ticker, "action": desired_action, "order_placed": False, "dry_run": True, "reasoning": reasoning}
            return {"ticker": ticker, "action": "HOLD", "order_placed": False}

        if run is not None:
            self.runtime.update_run(session, run, stage="pricing", current_ticker=ticker, current_agent=None)
            session.commit()
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
            self.runtime.update_run(session, run, stage="position_sizing", current_ticker=ticker, current_agent=None)
            session.commit()
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
            self.runtime.update_run(session, run, stage="placing_order", current_ticker=ticker, current_agent=None)
            session.commit()
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
