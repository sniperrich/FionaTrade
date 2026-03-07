from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.analysis.service import AnalysisService
from app.analysis.rules_fallback import fallback_action
from app.core.config import Settings
from app.core.logging import get_app_logger, log_writeout
from app.core.utils import ensure_utc, utc_now
from app.db.models import BacktestRun, BacktestTrade, Bar1m, Event, EventEvidence


@dataclass
class BacktestResult:
    run_id: int
    status: str
    metrics: dict


class BacktestEngineService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.analysis = AnalysisService(settings)
        self.logger = get_app_logger()

    @staticmethod
    def _as_bool(value: object, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return default

    def _bar_at_or_after(self, session: Session, ticker: str, ts) -> Bar1m | None:
        return session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()

    def _bars_between(self, session: Session, ticker: str, start_ts, end_ts) -> list[Bar1m]:
        return (
            session.execute(
                select(Bar1m)
                .where(
                    and_(
                        Bar1m.ticker == ticker,
                        Bar1m.ts > start_ts,
                        Bar1m.ts <= end_ts,
                    )
                )
                .order_by(Bar1m.ts.asc())
            )
            .scalars()
            .all()
        )

    def _apply_slippage(self, price: float, side: str, leg: str, slippage_bps: float | None = None) -> float:
        bps = self.settings.default_slippage_bps if slippage_bps is None else max(float(slippage_bps), 0.0)
        slip = bps / 10_000.0
        if side == "LONG":
            return price * (1 + slip) if leg == "entry" else price * (1 - slip)
        if side == "SHORT":
            return price * (1 - slip) if leg == "entry" else price * (1 + slip)
        return price

    def _position_size(
        self,
        nav: float,
        entry_px: float,
        stop_loss_pct: float,
        risk_per_trade_pct: float,
        risk_sizing: bool,
    ) -> float:
        if nav <= 0:
            return 0.0

        cap_notional = nav * self.settings.max_position_pct
        qty_cap = cap_notional / max(entry_px, 0.01)
        if not risk_sizing:
            return max(qty_cap, 0.0)

        risk_budget = nav * max(risk_per_trade_pct, 0.0)
        risk_per_share = max(entry_px * max(stop_loss_pct, 0.0001), 0.01)
        qty_risk = risk_budget / risk_per_share
        return max(min(qty_cap, qty_risk), 0.0)

    def _first_barrier_hit(
        self,
        session: Session,
        ticker: str,
        entry_ts,
        end_ts,
        side: str,
        entry_px: float,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> tuple[datetime, float, str] | None:
        if stop_loss_pct <= 0 and take_profit_pct <= 0:
            return None

        bars = self._bars_between(session, ticker, entry_ts, end_ts)
        if not bars:
            return None

        if side == "LONG":
            stop_px = entry_px * (1 - stop_loss_pct) if stop_loss_pct > 0 else None
            take_px = entry_px * (1 + take_profit_pct) if take_profit_pct > 0 else None

            for bar in bars:
                stop_hit = bool(stop_px is not None and bar.low <= stop_px)
                take_hit = bool(take_px is not None and bar.high >= take_px)
                if stop_hit and take_hit:
                    # Conservative assumption for same-candle touch.
                    return bar.ts, stop_px, "STOP"
                if stop_hit:
                    return bar.ts, stop_px, "STOP"
                if take_hit:
                    return bar.ts, take_px, "TAKE"
            return None

        if side == "SHORT":
            stop_px = entry_px * (1 + stop_loss_pct) if stop_loss_pct > 0 else None
            take_px = entry_px * (1 - take_profit_pct) if take_profit_pct > 0 else None

            for bar in bars:
                stop_hit = bool(stop_px is not None and bar.high >= stop_px)
                take_hit = bool(take_px is not None and bar.low <= take_px)
                if stop_hit and take_hit:
                    return bar.ts, stop_px, "STOP"
                if stop_hit:
                    return bar.ts, stop_px, "STOP"
                if take_hit:
                    return bar.ts, take_px, "TAKE"
            return None

        return None

    def _compute_metrics(self, initial_nav: float, equity_curve: list[dict], pnl_list: list[float]) -> dict:
        final_nav = equity_curve[-1]["equity"] if equity_curve else initial_nav
        total_return = (final_nav - initial_nav) / initial_nav if initial_nav else 0.0

        minutes = max(1, len(equity_curve))
        annualization_factor = (252 * 390) / minutes
        annualized_return = (1 + total_return) ** annualization_factor - 1 if total_return > -1 else -1

        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]["equity"]
            curr = equity_curve[i]["equity"]
            returns.append((curr - prev) / prev if prev else 0.0)

        if returns and pstdev(returns) > 0:
            sharpe = (mean(returns) / pstdev(returns)) * math.sqrt(252 * 390)
        else:
            sharpe = 0.0

        peak = initial_nav
        max_dd = 0.0
        for p in equity_curve:
            peak = max(peak, p["equity"])
            dd = (p["equity"] - peak) / peak if peak else 0.0
            max_dd = min(max_dd, dd)

        wins = [x for x in pnl_list if x > 0]
        losses = [x for x in pnl_list if x < 0]
        win_rate = (len(wins) / len(pnl_list)) if pnl_list else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)

        avg_win = mean(wins) if wins else 0.0
        avg_loss = abs(mean(losses)) if losses else 0.0
        pnl_ratio = (avg_win / avg_loss) if avg_loss else 0.0

        return {
            "total_return": total_return,
            "annualized_return": annualized_return,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "pnl_ratio": pnl_ratio,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "largest_win": max(wins) if wins else 0.0,
            "largest_loss": min(losses) if losses else 0.0,
            "trades": len(pnl_list),
        }

    def run(self, session: Session, params: dict | None = None) -> BacktestResult:
        params = params or {}
        horizon_min = int(params.get("horizon_min", self.settings.default_horizon_min))
        min_conf = int(params.get("min_confidence", self.settings.min_trade_confidence))
        use_llm = self._as_bool(params.get("use_llm"), default=False)
        use_signal_horizon = self._as_bool(params.get("use_signal_horizon"), default=True)
        progress_every = int(params.get("progress_every", 10))
        if progress_every < 1:
            progress_every = 1

        stop_loss_pct = float(params.get("stop_loss_pct", self.settings.stop_loss_pct))
        take_profit_pct = float(params.get("take_profit_pct", self.settings.take_profit_pct))
        slippage_bps = float(params.get("slippage_bps", self.settings.default_slippage_bps))
        hard_stops = self._as_bool(params.get("hard_stops"), default=self.settings.backtest_hard_stops)
        risk_sizing = self._as_bool(params.get("risk_sizing"), default=self.settings.backtest_risk_sizing)
        risk_per_trade_pct = float(params.get("risk_per_trade_pct", self.settings.backtest_risk_per_trade_pct))
        daily_circuit_breaker = self._as_bool(
            params.get("daily_circuit_breaker"),
            default=self.settings.backtest_daily_circuit_breaker,
        )
        enable_term_horizon = self._as_bool(
            params.get("enable_term_horizon"),
            default=self.settings.backtest_enable_term_horizon,
        )

        start_date = params.get("start_date")
        end_date = params.get("end_date")

        run = BacktestRun(params=params, status="RUNNING")
        session.add(run)
        session.flush()

        stmt = select(Event).where(
            and_(
                Event.validation_status == "VALID",
                Event.confidence >= min_conf,
            )
        )

        if start_date:
            start_dt = ensure_utc(datetime.fromisoformat(str(start_date)))
            stmt = stmt.where(Event.event_time >= start_dt)
        if end_date:
            end_dt = ensure_utc(datetime.fromisoformat(str(end_date)))
            stmt = stmt.where(Event.event_time < end_dt)

        events = session.execute(stmt.order_by(Event.event_time.asc())).scalars().all()
        started = time.perf_counter()

        self.logger.info(
            "回测开始 run_id=%s events=%s use_llm=%s min_conf=%s horizon=%s start=%s end=%s",
            run.id,
            len(events),
            use_llm,
            min_conf,
            horizon_min,
            start_date,
            end_date,
        )
        log_writeout(
            "backtest_started",
            {
                "run_id": run.id,
                "events": len(events),
                "use_llm": use_llm,
                "min_confidence": min_conf,
                "horizon_min": horizon_min,
                "start_date": start_date,
                "end_date": end_date,
                "hard_stops": hard_stops,
                "risk_sizing": risk_sizing,
                "risk_per_trade_pct": risk_per_trade_pct,
                "slippage_bps": slippage_bps,
                "daily_circuit_breaker": daily_circuit_breaker,
                "enable_term_horizon": enable_term_horizon,
            },
        )

        equity = self.settings.initial_nav
        equity_curve = [{"ts": utc_now().isoformat(), "equity": equity}]
        pnl_list: list[float] = []
        trade_log: list[dict] = []
        event_type_attr: dict[str, float] = {}
        source_attr: dict[str, float] = {}
        llm_signals = 0
        llm_fallback_signals = 0
        exit_reason_counts: dict[str, int] = {}
        daily_halts = 0
        halted_events_skipped = 0

        current_day: date | None = None
        day_start_equity = equity
        day_halted = False

        total_events = len(events)

        def emit_progress(idx: int) -> None:
            if idx % progress_every != 0 and idx != total_events:
                return
            elapsed = time.perf_counter() - started
            progress_pct = (idx / total_events * 100.0) if total_events else 100.0
            self.logger.info(
                "回测进度 run_id=%s %.1f%%(%s/%s) trades=%s llm_signals=%s fallback=%s equity=%.2f elapsed=%.1fs",
                run.id,
                progress_pct,
                idx,
                total_events,
                len(pnl_list),
                llm_signals,
                llm_fallback_signals,
                equity,
                elapsed,
            )
            log_writeout(
                "backtest_progress",
                {
                    "run_id": run.id,
                    "progress": idx,
                    "total": total_events,
                    "progress_pct": progress_pct,
                    "trades": len(pnl_list),
                    "llm_signals": llm_signals,
                    "llm_fallback_signals": llm_fallback_signals,
                    "equity": equity,
                    "elapsed_sec": round(elapsed, 2),
                    "daily_halts": daily_halts,
                    "halted_events_skipped": halted_events_skipped,
                },
            )

        if total_events == 0:
            emit_progress(0)

        for idx, event in enumerate(events, start=1):
            event_ts = ensure_utc(event.event_time)
            event_day = event_ts.date()
            if current_day != event_day:
                current_day = event_day
                day_start_equity = equity
                day_halted = False

            if daily_circuit_breaker and day_start_equity > 0:
                day_pnl_pct = (equity - day_start_equity) / day_start_equity
                if day_pnl_pct <= self.settings.daily_loss_limit_pct:
                    if not day_halted:
                        daily_halts += 1
                        self.logger.warning(
                            "回测日内熔断 run_id=%s day=%s day_pnl_pct=%.4f limit=%.4f",
                            run.id,
                            event_day.isoformat(),
                            day_pnl_pct,
                            self.settings.daily_loss_limit_pct,
                        )
                        log_writeout(
                            "backtest_daily_halt",
                            {
                                "run_id": run.id,
                                "day": event_day.isoformat(),
                                "day_pnl_pct": day_pnl_pct,
                                "limit_pct": self.settings.daily_loss_limit_pct,
                            },
                        )
                    day_halted = True

            if day_halted:
                halted_events_skipped += 1
                emit_progress(idx)
                continue

            if not event.tickers:
                emit_progress(idx)
                continue

            ticker = event.tickers[0]
            local_horizon_min = horizon_min
            fallback_used = False

            if use_llm:
                self.logger.info(
                    "回测LLM处理 run_id=%s progress=%s/%s event_id=%s ticker=%s",
                    run.id,
                    idx,
                    total_events,
                    event.id,
                    ticker,
                )
                signal = self.analysis.event_to_signal(event, session=session)
                if not signal:
                    emit_progress(idx)
                    continue
                llm_signals += 1
                fallback_used = signal.fallback_used
                if fallback_used:
                    llm_fallback_signals += 1
                action = signal.action
                ticker = signal.ticker or ticker
                if use_signal_horizon and signal.horizon_min > 0:
                    if signal.horizon_profile and not enable_term_horizon:
                        local_horizon_min = horizon_min
                    else:
                        local_horizon_min = int(signal.horizon_min)
            else:
                action = fallback_action(event.event_type)

            if action == "HOLD":
                emit_progress(idx)
                continue

            entry_bar = self._bar_at_or_after(session, ticker, event_ts + timedelta(minutes=1))
            planned_exit_bar = self._bar_at_or_after(session, ticker, event_ts + timedelta(minutes=local_horizon_min))
            if not entry_bar or not planned_exit_bar:
                emit_progress(idx)
                continue

            side = "LONG" if action == "BUY" else "SHORT"
            entry_px = self._apply_slippage(float(entry_bar.open), side=side, leg="entry", slippage_bps=slippage_bps)

            qty = self._position_size(
                nav=equity,
                entry_px=entry_px,
                stop_loss_pct=stop_loss_pct,
                risk_per_trade_pct=risk_per_trade_pct,
                risk_sizing=risk_sizing,
            )
            if qty <= 0:
                emit_progress(idx)
                continue

            exit_ts = planned_exit_bar.ts
            exit_base_px = float(planned_exit_bar.close)
            exit_reason = "HORIZON"

            if hard_stops:
                hit = self._first_barrier_hit(
                    session=session,
                    ticker=ticker,
                    entry_ts=entry_bar.ts,
                    end_ts=exit_ts,
                    side=side,
                    entry_px=entry_px,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                if hit:
                    exit_ts, exit_base_px, exit_reason = hit

            exit_px = self._apply_slippage(exit_base_px, side=side, leg="exit", slippage_bps=slippage_bps)

            notional = qty * entry_px
            if side == "LONG":
                pnl = qty * (exit_px - entry_px)
            else:
                pnl = qty * (entry_px - exit_px)

            holding_min = max(
                1.0,
                (ensure_utc(exit_ts) - ensure_utc(entry_bar.ts)).total_seconds() / 60.0,
            )
            if side == "SHORT":
                borrow_cost = notional * self.settings.short_borrow_apr * (holding_min / (365 * 24 * 60))
                pnl -= borrow_cost

            equity += pnl
            pnl_list.append(pnl)

            exit_reason_counts[exit_reason] = exit_reason_counts.get(exit_reason, 0) + 1

            trade_entry = {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "entry_ts": entry_bar.ts.isoformat(),
                "entry_price": entry_px,
                "exit_ts": exit_ts.isoformat(),
                "exit_price": exit_px,
                "pnl": pnl,
                "event_type": event.event_type,
                "event_id": event.id,
                "horizon_min": local_horizon_min,
                "holding_min": holding_min,
                "fallback_used": fallback_used,
                "exit_reason": exit_reason,
            }
            trade_log.append(trade_entry)
            equity_curve.append({"ts": exit_ts.isoformat(), "equity": equity})

            session.add(
                BacktestTrade(
                    run_id=run.id,
                    signal_id=None,
                    ticker=ticker,
                    side=side,
                    qty=qty,
                    entry_ts=entry_bar.ts,
                    entry_price=entry_px,
                    exit_ts=exit_ts,
                    exit_price=exit_px,
                    pnl=pnl,
                    event_type=event.event_type,
                )
            )

            event_type_attr[event.event_type] = event_type_attr.get(event.event_type, 0.0) + pnl
            evidence = session.execute(
                select(EventEvidence).where(EventEvidence.event_id == event.id).order_by(EventEvidence.id.asc()).limit(1)
            ).scalar_one_or_none()
            if evidence:
                source_attr[evidence.source] = source_attr.get(evidence.source, 0.0) + pnl

            emit_progress(idx)

        metrics = self._compute_metrics(self.settings.initial_nav, equity_curve, pnl_list)
        metrics["events_considered"] = len(events)
        metrics["event_type_attribution"] = event_type_attr
        metrics["source_attribution"] = source_attr
        metrics["use_llm"] = use_llm
        metrics["llm_model"] = self.settings.llm_model if use_llm else ""
        metrics["model_version"] = str(params.get("model_version") or self.settings.llm_model or "")
        metrics["llm_signals"] = llm_signals
        metrics["llm_fallback_signals"] = llm_fallback_signals
        metrics["hard_stops"] = hard_stops
        metrics["risk_sizing"] = risk_sizing
        metrics["risk_per_trade_pct"] = risk_per_trade_pct
        metrics["slippage_bps"] = slippage_bps
        metrics["exit_reason_counts"] = exit_reason_counts
        metrics["daily_halts"] = daily_halts
        metrics["halted_events_skipped"] = halted_events_skipped
        metrics["enable_term_horizon"] = enable_term_horizon

        run.metrics = metrics
        run.equity_curve = equity_curve
        run.trade_log = trade_log
        run.status = "DONE"
        run.finished_at = utc_now()

        elapsed = time.perf_counter() - started
        self.logger.info(
            "回测完成 run_id=%s trades=%s total_return=%.4f llm_signals=%s fallback=%s elapsed=%.1fs",
            run.id,
            metrics.get("trades", 0),
            metrics.get("total_return", 0.0),
            llm_signals,
            llm_fallback_signals,
            elapsed,
        )
        log_writeout(
            "backtest_finished",
            {
                "run_id": run.id,
                "trades": metrics.get("trades", 0),
                "total_return": metrics.get("total_return", 0.0),
                "llm_signals": llm_signals,
                "llm_fallback_signals": llm_fallback_signals,
                "elapsed_sec": round(elapsed, 2),
            },
        )

        return BacktestResult(run_id=run.id, status=run.status, metrics=metrics)

    def get_run(self, session: Session, run_id: int) -> BacktestRun | None:
        return session.execute(select(BacktestRun).where(BacktestRun.id == run_id)).scalar_one_or_none()
