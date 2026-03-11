from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.analysis.service import AnalysisService
from app.analysis.rules_fallback import fallback_action
from app.analysis.signal_validator import (
    ExecutionRecommendation,
    SignalValidator,
)
from app.core.config import Settings
from app.core.logging import ensure_logging, get_app_logger, log_writeout
from app.core.utils import ensure_utc, utc_now
from app.db.models import BacktestRun, BacktestTrade, Bar1m, Event, EventEvidence


@dataclass
class BacktestResult:
    run_id: int
    status: str
    metrics: dict


class BacktestEngineService:
    _ROUTINE_FILING_RE = re.compile(
        r"\bfiled\s+(?:form\s+)?(?:8-k|10-k|10-q|6-k|13d|13g|sc\s*13d|sc\s*13g)\b",
        re.IGNORECASE,
    )
    _MATERIAL_FILING_MARKERS = (
        "restatement",
        "material weakness",
        "internal control",
        "bankrupt",
        "chapter 11",
        "investigation",
        "sec charge",
        "doj",
        "fraud",
        "guidance cut",
        "lowered outlook",
        "earnings miss",
        "missed estimates",
        "major litigation",
        "class action",
        "accident",
        "explosion",
        "fire",
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        ensure_logging(log_dir=settings.log_dir, log_level=settings.log_level)
        self.analysis = AnalysisService(settings)
        self.validator = SignalValidator(settings)
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

    @staticmethod
    def _pct_change(base: float | None, current: float | None) -> float | None:
        if base is None or current is None or abs(base) < 1e-9:
            return None
        return (current - base) / base

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
        position_pct_suggestion: float | None = None,
    ) -> float:
        if nav <= 0:
            return 0.0

        cap_notional = nav * self.settings.max_position_pct
        qty_cap = cap_notional / max(entry_px, 0.01)
        qty_limits = [qty_cap]

        if risk_sizing:
            risk_budget = nav * max(risk_per_trade_pct, 0.0)
            risk_per_share = max(entry_px * max(stop_loss_pct, 0.0001), 0.01)
            qty_risk = risk_budget / risk_per_share
            qty_limits.append(qty_risk)

        if position_pct_suggestion is not None:
            suggested = max(0.0, min(1.0, float(position_pct_suggestion)))
            suggested_notional = cap_notional * suggested
            qty_limits.append(suggested_notional / max(entry_px, 0.01))

        return max(min(qty_limits), 0.0)

    def _effective_position_pct_suggestion(
        self,
        event: Event,
        position_pct_suggestion: float | None,
        tradeability_score: int | None,
        conviction_position_sizing: bool,
        risk_sizing: bool,
    ) -> float | None:
        if not conviction_position_sizing or not risk_sizing:
            return position_pct_suggestion

        raw = None if position_pct_suggestion is None else max(0.0, min(1.0, float(position_pct_suggestion)))
        score = max(0, min(int(tradeability_score or 0), 100))
        confidence = max(0, min(int(event.confidence or 0), 100))
        severity = max(0, min(int(event.severity or 0), 100))

        floor = 0.0
        base_floor = max(0.0, min(1.0, float(self.settings.backtest_conviction_position_floor)))
        if score >= 80 and confidence >= 75 and severity >= 60:
            floor = min(1.0, base_floor + 0.20)
        elif score >= 70 and confidence >= 70 and severity >= 55:
            floor = base_floor
        elif score >= 60 and confidence >= 65 and severity >= 50:
            floor = max(0.0, base_floor - 0.10)

        if raw is None:
            return floor if floor > 0 else None
        return max(raw, floor)

    def _conviction_risk_multiplier(
        self,
        event: Event,
        position_pct_suggestion: float | None,
        tradeability_score: int | None,
        conviction_position_sizing: bool,
    ) -> float:
        if not conviction_position_sizing:
            return 1.0

        min_mult = max(0.1, float(self.settings.backtest_conviction_min_risk_multiplier))
        max_mult = max(min_mult, float(self.settings.backtest_conviction_max_risk_multiplier))
        confidence_component = max(0.0, min(float(event.confidence or 0) / 100.0, 1.0))
        severity_component = max(0.0, min(float(event.severity or 0) / 100.0, 1.0))
        tradeability_component = max(0.0, min(float(tradeability_score or 0) / 100.0, 1.0))
        size_component = 0.35 if position_pct_suggestion is None else max(0.0, min(float(position_pct_suggestion), 1.0))
        composite = (
            0.35 * tradeability_component
            + 0.25 * confidence_component
            + 0.20 * severity_component
            + 0.20 * size_component
        )
        return min_mult + (max_mult - min_mult) * composite

    def _is_routine_filing_event(self, event: Event) -> bool:
        summary = (event.summary or "").lower()
        if not summary:
            return False
        if not self._ROUTINE_FILING_RE.search(summary):
            return False
        if any(marker in summary for marker in self._MATERIAL_FILING_MARKERS):
            return False
        return True

    @staticmethod
    def _is_event_excluded(event_type: str | None, use_llm: bool, allow_unknown_with_llm: bool) -> bool:
        from app.analysis.taxonomy import EXCLUDED_FROM_TRADING

        et = (event_type or "").strip()
        if et == "unknown" and use_llm and allow_unknown_with_llm:
            return False
        return et in EXCLUDED_FROM_TRADING

    def _is_first_bar_of_day(self, session: Session, ticker: str, ts: datetime) -> bool:
        bar_ts = ensure_utc(ts)
        day_start = bar_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        first = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= day_start, Bar1m.ts < day_end))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not first:
            return False
        return ensure_utc(first.ts) == bar_ts

    def _market_regime_for_event(self, session: Session, event_ts: datetime) -> tuple[str, float | None]:
        """Infer broad market regime from SPY return over ~20 trading days."""
        start_ts = ensure_utc(event_ts) - timedelta(days=28)
        start_bar = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == "SPY", Bar1m.ts >= start_ts, Bar1m.ts <= event_ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()
        end_bar = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == "SPY", Bar1m.ts <= event_ts))
            .order_by(Bar1m.ts.desc())
            .limit(1)
        ).scalar_one_or_none()

        if not start_bar or not end_bar:
            return "NEUTRAL", None

        ret = self._pct_change(float(start_bar.close), float(end_bar.close))
        if ret is None:
            return "NEUTRAL", None
        if ret >= 0.03:
            return "BULL", ret
        if ret <= -0.03:
            return "BEAR", ret
        return "NEUTRAL", ret

    def _deduplicate_same_day_events(self, events: list[Event]) -> list[Event]:
        """Keep only the highest-severity event for (ticker, event_type, day)."""
        selected: dict[tuple[str, str, date], Event] = {}
        passthrough: list[Event] = []

        for event in events:
            if not event.tickers:
                passthrough.append(event)
                continue
            event_ts = ensure_utc(event.event_time)
            key = (str(event.tickers[0]).upper(), event.event_type or "", event_ts.date())
            current = selected.get(key)
            if current is None:
                selected[key] = event
                continue
            current_ts = ensure_utc(current.event_time)
            new_rank = ((event.severity or 0), (event.confidence or 0), event_ts.timestamp())
            current_rank = ((current.severity or 0), (current.confidence or 0), current_ts.timestamp())
            if new_rank > current_rank:
                selected[key] = event

        deduped = list(selected.values()) + passthrough
        deduped.sort(key=lambda e: ensure_utc(e.event_time))
        return deduped

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
        min_severity = int(params.get("min_severity", 0))  # 0 = no filter; 70 = strong events only
        use_llm = self._as_bool(params.get("use_llm"), default=False)
        use_signal_horizon = self._as_bool(params.get("use_signal_horizon"), default=True)
        progress_every = int(params.get("progress_every", 10))
        if progress_every < 1:
            progress_every = 1
        llm_workers = int(params.get("llm_workers", 8))

        stop_loss_pct = float(params.get("stop_loss_pct", self.settings.stop_loss_pct))
        take_profit_pct = float(params.get("take_profit_pct", self.settings.take_profit_pct))
        slippage_bps = float(params.get("slippage_bps", self.settings.default_slippage_bps))
        hard_stops = self._as_bool(params.get("hard_stops"), default=self.settings.backtest_hard_stops)
        risk_sizing = self._as_bool(params.get("risk_sizing"), default=self.settings.backtest_risk_sizing)
        risk_per_trade_pct = float(params.get("risk_per_trade_pct", self.settings.backtest_risk_per_trade_pct))
        entry_window_min = int(params.get("entry_window_min", self.settings.backtest_entry_window_min))
        if entry_window_min < 1:
            entry_window_min = 60
        daily_circuit_breaker = self._as_bool(
            params.get("daily_circuit_breaker"),
            default=self.settings.backtest_daily_circuit_breaker,
        )
        regime_risk_adjust = self._as_bool(
            params.get("regime_risk_adjust"),
            default=self.settings.backtest_regime_risk_adjust,
        )
        regime_bull_risk_multiplier = float(
            params.get("regime_bull_risk_multiplier", self.settings.backtest_regime_bull_risk_multiplier)
        )
        regime_bear_risk_multiplier = float(
            params.get("regime_bear_risk_multiplier", self.settings.backtest_regime_bear_risk_multiplier)
        )
        dedup_same_day_event = self._as_bool(
            params.get("dedup_same_day_event"),
            default=self.settings.backtest_dedup_same_day_event,
        )
        enable_term_horizon = self._as_bool(
            params.get("enable_term_horizon"),
            default=self.settings.backtest_enable_term_horizon,
        )
        allow_unknown_with_llm = self._as_bool(
            params.get("allow_unknown_with_llm"),
            default=self.settings.backtest_allow_unknown_with_llm,
        )
        allow_next_session_entry = self._as_bool(
            params.get("allow_next_session_entry"),
            default=self.settings.backtest_allow_next_session_entry,
        )
        use_tradeability_filter = self._as_bool(
            params.get("use_tradeability_filter"),
            default=self.settings.event_tradeability_filter_enabled,
        )
        tradeability_min_score = int(
            params.get("tradeability_min_score", self.settings.event_tradeability_min_score)
        )
        use_event_quality_filter = self._as_bool(
            params.get("use_event_quality_filter"),
            default=self.settings.backtest_use_event_quality_filter,
        )
        event_quality_min_score = int(
            params.get("event_quality_min_score", self.settings.backtest_event_quality_min_score)
        )
        event_quality_fail_open = self._as_bool(
            params.get("event_quality_fail_open"),
            default=self.settings.backtest_event_quality_fail_open,
        )
        conviction_position_sizing = self._as_bool(
            params.get("conviction_position_sizing"),
            default=self.settings.backtest_conviction_position_sizing,
        )
        # Signal Validation Layer: on by default when validation_enabled=True in settings
        use_signal_validation = self._as_bool(
            params.get("use_signal_validation"),
            default=getattr(self.settings, "validation_enabled", True),
        )
        validation_min_score = int(
            params.get("validation_min_review_score",
                       getattr(self.settings, "validation_min_review_score", 40))
        )
        allow_downweight = self._as_bool(
            params.get("validation_allow_downweight_execution"),
            default=getattr(self.settings, "validation_allow_downweight_execution", True),
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
        events_before_dedup = len(events)
        if dedup_same_day_event:
            events = self._deduplicate_same_day_events(events)
        dedup_dropped = max(0, events_before_dedup - len(events))
        started = time.perf_counter()

        self.logger.info(
            "回测开始 run_id=%s events=%s dedup_dropped=%s use_llm=%s min_conf=%s horizon=%s entry_window=%s start=%s end=%s",
            run.id,
            len(events),
            dedup_dropped,
            use_llm,
            min_conf,
            horizon_min,
            entry_window_min,
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
                "entry_window_min": entry_window_min,
                "hard_stops": hard_stops,
                "risk_sizing": risk_sizing,
                "risk_per_trade_pct": risk_per_trade_pct,
                "regime_risk_adjust": regime_risk_adjust,
                "regime_bull_risk_multiplier": regime_bull_risk_multiplier,
                "regime_bear_risk_multiplier": regime_bear_risk_multiplier,
                "dedup_same_day_event": dedup_same_day_event,
                "dedup_dropped": dedup_dropped,
                "slippage_bps": slippage_bps,
                "daily_circuit_breaker": daily_circuit_breaker,
                "enable_term_horizon": enable_term_horizon,
                "allow_unknown_with_llm": allow_unknown_with_llm,
                "allow_next_session_entry": allow_next_session_entry,
                "use_tradeability_filter": use_tradeability_filter,
                "tradeability_min_score": tradeability_min_score,
                "use_event_quality_filter": use_event_quality_filter,
                "event_quality_min_score": event_quality_min_score,
                "event_quality_fail_open": event_quality_fail_open,
                "conviction_position_sizing": conviction_position_sizing,
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
        validation_blocked = 0
        exit_reason_counts: dict[str, int] = {}
        regime_trade_counts: dict[str, int] = {}
        daily_halts = 0
        halted_events_skipped = 0
        routine_filing_skipped = 0
        tradeability_filtered = 0
        quality_filtered = 0
        quality_filter_errors = 0
        entry_late_skipped = 0
        next_session_entry_used = 0
        tradeability_reason_counts: dict[str, int] = {}

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

        def tradeability_result(event: Event) -> dict[str, object] | None:
            if not use_tradeability_filter:
                return None
            result = self.analysis.assess_tradeability(event, session=session)
            score = int(result.get("score", 0))
            if result.get("tradeable", True) and score < tradeability_min_score:
                result = dict(result)
                result["tradeable"] = False
                result["reason"] = "tradeability_score_too_low"
            return result

        # ── 并发 LLM 预取阶段 ──────────────────────────────────────────────────
        # 先用线程池并发获取所有 LLM 信号，再串行执行交易逻辑（保证 equity 顺序正确）
        signal_map: dict[int, object] = {}  # event.id → TradeSignal | None
        if use_llm and total_events > 0:
            _MACRO_NOISE_PREFETCH = (
                "government shutdown", "stock market today", "market movers",
                "wall street lunch", "s&p 500", "dow jones futures",
                "market summary", "early movers", "morning movers",
                "ftse 100", "equity indexes", "equity futures",
                "hits new high", "hits all-time high", "worth a look",
                "markets look past", "look past shutdown", "no government",
                "bitcoin price", "crypto", "jobs report",
            )
            tradeable_events = [
                e for e in events
                if e.tickers and not self._is_event_excluded(e.event_type, use_llm=use_llm, allow_unknown_with_llm=allow_unknown_with_llm)
                and (min_severity == 0 or (e.severity or 0) >= min_severity)
                and not self._is_routine_filing_event(e)
                and not any(p in (e.summary or "").lower() for p in _MACRO_NOISE_PREFETCH)
                and (not use_tradeability_filter or bool((tradeability_result(e) or {}).get("tradeable", True)))
            ]
            # Step 1: 预热 Finnhub 补充数据 cache（串行，避免 429）
            unique_tickers = list({str(e.tickers[0]).upper() for e in tradeable_events if e.tickers})
            self.logger.info(
                "预热Finnhub cache run_id=%s unique_tickers=%d",
                run.id, len(unique_tickers),
            )
            for i, tk in enumerate(unique_tickers):
                self.analysis._finnhub_earnings_context(tk)
                self.analysis._finnhub_tech_signal(tk)
                self.analysis._finnhub_support_resistance(tk, None)
                self.analysis._finnhub_analyst_consensus(tk)
                if (i + 1) % 10 == 0 or (i + 1) == len(unique_tickers):
                    self.logger.info("Finnhub cache预热 %d/%d", i + 1, len(unique_tickers))

            self.logger.info(
                "并发LLM预取 run_id=%s workers=%s tradeable=%s/%s",
                run.id, llm_workers, len(tradeable_events), total_events,
            )
            llm_started = time.perf_counter()
            completed_count = 0

            def _fetch_signal(ev):
                return ev.id, self.analysis.event_to_signal(
                    ev,
                    session=session,
                    use_tradeability_filter=use_tradeability_filter,
                )

            with ThreadPoolExecutor(max_workers=llm_workers) as pool:
                futures = {pool.submit(_fetch_signal, ev): ev for ev in tradeable_events}
                for fut in as_completed(futures):
                    try:
                        eid, sig = fut.result()
                        signal_map[eid] = sig
                    except Exception as exc:
                        ev = futures[fut]
                        self.logger.warning("LLM预取失败 event_id=%s: %s", ev.id, exc)
                        signal_map[ev.id] = None
                    completed_count += 1
                    if completed_count % max(1, llm_workers * 4) == 0 or completed_count == len(tradeable_events):
                        elapsed_llm = time.perf_counter() - llm_started
                        rate = completed_count / elapsed_llm if elapsed_llm > 0 else 0
                        self.logger.info(
                            "LLM预取进度 %d/%d (%.1f/s) eta=%.0fs",
                            completed_count, len(tradeable_events),
                            rate,
                            (len(tradeable_events) - completed_count) / rate if rate > 0 else 0,
                        )
            self.logger.info(
                "LLM预取完成 run_id=%s signals=%d elapsed=%.1fs",
                run.id, len(signal_map), time.perf_counter() - llm_started,
            )

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

            if self._is_event_excluded(event.event_type, use_llm=use_llm, allow_unknown_with_llm=allow_unknown_with_llm):
                emit_progress(idx)
                continue

            if self._is_routine_filing_event(event):
                routine_filing_skipped += 1
                emit_progress(idx)
                continue

            # Event strength filter: skip weak single-source events below severity threshold
            if min_severity > 0 and (event.severity or 0) < min_severity:
                emit_progress(idx)
                continue

            # Summary noise filter: skip broad market-round-up events (no ticker-specific signal)
            _summary_lower = (event.summary or "").lower()
            _MACRO_NOISE = (
                "government shutdown", "stock market today", "market movers",
                "wall street lunch", "s&p 500", "dow jones futures",
                "market summary", "early movers", "morning movers",
                "ftse 100", "equity indexes", "equity futures",
                "hits new high", "hits all-time high", "worth a look",
                "markets look past", "look past shutdown", "no government",
                "bitcoin price", "crypto", "jobs report",
            )
            if any(phrase in _summary_lower for phrase in _MACRO_NOISE):
                emit_progress(idx)
                continue

            ticker = event.tickers[0]
            local_horizon_min = horizon_min
            fallback_used = False
            position_pct_suggestion: float | None = None
            effective_position_pct_suggestion: float | None = None
            conviction_risk_multiplier = 1.0
            tradeability_score: int | None = None
            tradeability_reason: str | None = None

            if use_tradeability_filter:
                tradeability = tradeability_result(event)
                tradeability_score = int((tradeability or {}).get("score", 0))
                tradeability_reason = str((tradeability or {}).get("reason", "tradeable"))
                if tradeability and not bool(tradeability.get("tradeable", True)):
                    tradeability_filtered += 1
                    tradeability_reason_counts[tradeability_reason] = (
                        tradeability_reason_counts.get(tradeability_reason, 0) + 1
                    )
                    emit_progress(idx)
                    continue

            if use_llm:
                signal = signal_map.get(event.id)
                if not signal:
                    emit_progress(idx)
                    continue
                llm_signals += 1
                fallback_used = signal.fallback_used
                if fallback_used:
                    llm_fallback_signals += 1
                action = signal.action
                ticker = signal.ticker or ticker
                position_pct_suggestion = signal.position_pct_suggestion
                if use_signal_horizon and signal.horizon_min > 0:
                    if signal.horizon_profile and not enable_term_horizon:
                        local_horizon_min = horizon_min
                    else:
                        local_horizon_min = int(signal.horizon_min)
            else:
                action = fallback_action(event.event_type)

            if use_event_quality_filter:
                quality = self.analysis.assess_event_quality(event, session=session)
                quality_error = bool(quality.get("error"))
                if quality_error:
                    quality_filter_errors += 1

                if not (quality_error and event_quality_fail_open):
                    quality_score = int(quality.get("quality_score", 0))
                    if quality_score < event_quality_min_score:
                        quality_filtered += 1
                        emit_progress(idx)
                        continue

            if action == "HOLD":
                emit_progress(idx)
                continue

            if use_llm:
                effective_position_pct_suggestion = self._effective_position_pct_suggestion(
                    event=event,
                    position_pct_suggestion=position_pct_suggestion,
                    tradeability_score=tradeability_score,
                    conviction_position_sizing=conviction_position_sizing,
                    risk_sizing=risk_sizing,
                )
                conviction_risk_multiplier = self._conviction_risk_multiplier(
                    event=event,
                    position_pct_suggestion=effective_position_pct_suggestion,
                    tradeability_score=tradeability_score,
                    conviction_position_sizing=conviction_position_sizing and risk_sizing,
                )
            else:
                effective_position_pct_suggestion = position_pct_suggestion
                conviction_risk_multiplier = 1.0

            # ── Signal Validation Gate ────────────────────────────────────────
            if use_signal_validation and use_llm:
                _sig = signal_map.get(event.id)
                if _sig is not None:
                    _vr = self.validator.validate(
                        event=event,
                        signal=_sig,
                        price_context=None,
                        reference_time=ensure_utc(event.event_time),
                    )
                    _blocked = (
                        _vr.execution_recommendation
                        in (ExecutionRecommendation.REJECT, ExecutionRecommendation.NO_TRADE)
                        or _vr.review_score < validation_min_score
                        or (
                            _vr.execution_recommendation == ExecutionRecommendation.DOWNWEIGHT
                            and not allow_downweight
                        )
                    )
                    if _blocked:
                        validation_blocked += 1
                        self.logger.debug(
                            "validation_blocked ticker=%s score=%s rec=%s tags=%s",
                            _sig.ticker, _vr.review_score,
                            _vr.execution_recommendation.value,
                            _vr.issue_tags,
                        )
                        emit_progress(idx)
                        continue
            # ─────────────────────────────────────────────────────────────────

            entry_bar = self._bar_at_or_after(session, ticker, event_ts + timedelta(minutes=1))
            # Skip if no bar within configured entry window of event.
            if not entry_bar:
                emit_progress(idx)
                continue
            if ensure_utc(entry_bar.ts) > event_ts + timedelta(minutes=entry_window_min):
                allow_next_session = (
                    allow_next_session_entry
                    and self._is_first_bar_of_day(session, ticker, ensure_utc(entry_bar.ts))
                )
                if not allow_next_session:
                    entry_late_skipped += 1
                    emit_progress(idx)
                    continue
                next_session_entry_used += 1
            planned_exit_ts = ensure_utc(entry_bar.ts) + timedelta(minutes=local_horizon_min)
            planned_exit_bar = self._bar_at_or_after(session, ticker, planned_exit_ts)
            if not planned_exit_bar:
                emit_progress(idx)
                continue

            side = "LONG" if action == "BUY" else "SHORT"
            entry_px = self._apply_slippage(float(entry_bar.open), side=side, leg="entry", slippage_bps=slippage_bps)
            effective_risk_per_trade_pct = risk_per_trade_pct
            regime = "NEUTRAL"
            regime_multiplier = 1.0
            regime_return_pct = None
            if regime_risk_adjust:
                regime, regime_ret = self._market_regime_for_event(session, event_ts)
                regime_return_pct = (regime_ret * 100.0) if regime_ret is not None else None
                if regime == "BULL":
                    regime_multiplier = max(0.0, regime_bull_risk_multiplier)
                elif regime == "BEAR":
                    regime_multiplier = max(0.0, regime_bear_risk_multiplier)
                effective_risk_per_trade_pct = max(0.0, risk_per_trade_pct * regime_multiplier)
            effective_risk_per_trade_pct *= conviction_risk_multiplier

            qty = self._position_size(
                nav=equity,
                entry_px=entry_px,
                stop_loss_pct=stop_loss_pct,
                risk_per_trade_pct=effective_risk_per_trade_pct,
                risk_sizing=risk_sizing,
                position_pct_suggestion=effective_position_pct_suggestion,
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
                "risk_per_trade_pct": effective_risk_per_trade_pct,
                "regime": regime,
                "regime_multiplier": regime_multiplier,
                "regime_return_pct": regime_return_pct,
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
                "position_pct_suggestion": position_pct_suggestion,
                "effective_position_pct_suggestion": effective_position_pct_suggestion,
                "tradeability_score": tradeability_score,
                "tradeability_reason": tradeability_reason,
                "conviction_risk_multiplier": conviction_risk_multiplier,
                "exit_reason": exit_reason,
            }
            trade_log.append(trade_entry)
            equity_curve.append({"ts": exit_ts.isoformat(), "equity": equity})
            regime_trade_counts[regime] = regime_trade_counts.get(regime, 0) + 1

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
        metrics["entry_window_min"] = entry_window_min
        metrics["regime_risk_adjust"] = regime_risk_adjust
        metrics["regime_bull_risk_multiplier"] = regime_bull_risk_multiplier
        metrics["regime_bear_risk_multiplier"] = regime_bear_risk_multiplier
        metrics["dedup_same_day_event"] = dedup_same_day_event
        metrics["dedup_dropped"] = dedup_dropped
        metrics["regime_trade_counts"] = regime_trade_counts
        metrics["slippage_bps"] = slippage_bps
        metrics["exit_reason_counts"] = exit_reason_counts
        metrics["daily_halts"] = daily_halts
        metrics["halted_events_skipped"] = halted_events_skipped
        metrics["enable_term_horizon"] = enable_term_horizon
        metrics["validation_enabled"] = use_signal_validation
        metrics["validation_blocked"] = validation_blocked
        metrics["routine_filing_skipped"] = routine_filing_skipped
        metrics["use_tradeability_filter"] = use_tradeability_filter
        metrics["tradeability_min_score"] = tradeability_min_score
        metrics["tradeability_filtered"] = tradeability_filtered
        metrics["tradeability_reason_counts"] = tradeability_reason_counts
        metrics["use_event_quality_filter"] = use_event_quality_filter
        metrics["event_quality_min_score"] = event_quality_min_score
        metrics["event_quality_fail_open"] = event_quality_fail_open
        metrics["quality_filtered"] = quality_filtered
        metrics["quality_filter_errors"] = quality_filter_errors
        metrics["allow_unknown_with_llm"] = allow_unknown_with_llm
        metrics["allow_next_session_entry"] = allow_next_session_entry
        metrics["conviction_position_sizing"] = conviction_position_sizing
        metrics["conviction_min_risk_multiplier"] = self.settings.backtest_conviction_min_risk_multiplier
        metrics["conviction_max_risk_multiplier"] = self.settings.backtest_conviction_max_risk_multiplier
        metrics["conviction_position_floor"] = self.settings.backtest_conviction_position_floor
        metrics["entry_late_skipped"] = entry_late_skipped
        metrics["next_session_entry_used"] = next_session_entry_used

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
