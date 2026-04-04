from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.analysis.service import AnalysisService
from app.analysis.rules_fallback import fallback_action
from app.analysis.signal_validator import (
    ExecutionRecommendation,
    SignalValidator,
)
from app.analysis.taxonomy import (
    is_earnings_window_event,
    is_price_action_recap,
    normalize_source_name,
    resolve_event_type_for_text,
)
from app.core.config import Settings
from app.core.logging import ensure_logging, get_app_logger, log_writeout
from app.core.utils import ensure_utc, utc_now
from app.db.models import BacktestRun, BacktestTrade, Bar1m, Event
from app.services.capital_confirmation import CapitalConfirmationService


@dataclass
class BacktestResult:
    run_id: int
    status: str
    metrics: dict


class BacktestEngineService:
    _NY_TZ = ZoneInfo("America/New_York")
    _REGULAR_SESSION_OPEN = dt_time(9, 30)
    _REGULAR_SESSION_CLOSE = dt_time(16, 0)
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
    _EARNINGS_KEYWORD_RE = re.compile(
        r"\b(earnings|guidance|estimate(?:s)?|eps|revenue|sales|results?|quarter(?:ly)?|outlook|forecast)\b",
        re.IGNORECASE,
    )
    _EARNINGS_RELEASE_RE = re.compile(
        r"\b(reports?|reported|posts?|posted|results?|raises? (?:guidance|outlook|forecast)"
        r"|cuts? (?:guidance|outlook|forecast)|sales|revenue|eps|profit|income"
        r"|beats? (?:estimates|expectations)|missed? estimates|below expectations)\b",
        re.IGNORECASE,
    )
    _EARNINGS_EXCLUDE_RE = re.compile(
        r"\b(what to expect|ahead of earnings|to report|conference call|webcast|estimated value"
        r"|honest take|in the context of|due for a rally|has me excited|buy rating"
        r"|bullish consolidation|how to boost|takes center stage|earnings season"
        r"|release .* earnings|announces .* earnings|hold .* earnings|host .* earnings"
        r"|analyst questions|poised to beat|surprise streak|earnings release)\b",
        re.IGNORECASE,
    )
    _BACKTEST_PHASE_LABELS = {
        "queued": "Queued",
        "loading_events": "Loading Events",
        "cache_warmup": "Cache Warmup",
        "llm_prefetch": "LLM Prefetch",
        "event_execution": "Event Execution",
        "finalizing": "Finalizing",
        "completed": "Completed",
        "failed": "Failed",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        ensure_logging(log_dir=settings.log_dir, log_level=settings.log_level)
        self.analysis = AnalysisService(settings)
        self.validator = SignalValidator(settings)
        self.capital_confirmation = CapitalConfirmationService()
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

    @classmethod
    def _is_regular_session_bar(cls, ts: datetime) -> bool:
        local = ensure_utc(ts).astimezone(cls._NY_TZ)
        if local.weekday() >= 5:
            return False
        local_clock = local.timetz().replace(tzinfo=None)
        return cls._REGULAR_SESSION_OPEN <= local_clock < cls._REGULAR_SESSION_CLOSE

    @classmethod
    def _is_regular_session_open_bar(cls, ts: datetime) -> bool:
        local = ensure_utc(ts).astimezone(cls._NY_TZ)
        if local.weekday() >= 5:
            return False
        return local.hour == cls._REGULAR_SESSION_OPEN.hour and local.minute == cls._REGULAR_SESSION_OPEN.minute

    def _bar_at_or_after(
        self,
        session: Session,
        ticker: str,
        ts,
        regular_session_only: bool = False,
    ) -> Bar1m | None:
        stmt = (
            select(Bar1m)
            .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= ts))
            .order_by(Bar1m.ts.asc())
        )
        if not regular_session_only:
            return session.execute(stmt.limit(1)).scalar_one_or_none()

        search_end = ensure_utc(ts) + timedelta(days=5)
        bars = (
            session.execute(stmt.where(Bar1m.ts < search_end).limit(10000))
            .scalars()
            .all()
        )
        for bar in bars:
            if self._is_regular_session_bar(bar.ts):
                return bar
        return None

    def _bars_between(
        self,
        session: Session,
        ticker: str,
        start_ts,
        end_ts,
        regular_session_only: bool = False,
    ) -> list[Bar1m]:
        bars = (
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
        if not regular_session_only:
            return bars
        return [bar for bar in bars if self._is_regular_session_bar(bar.ts)]

    def _apply_slippage(self, price: float, side: str, leg: str, slippage_bps: float | None = None) -> float:
        bps = self.settings.default_slippage_bps if slippage_bps is None else max(float(slippage_bps), 0.0)
        slip = bps / 10_000.0
        if side == "LONG":
            return price * (1 + slip) if leg == "entry" else price * (1 - slip)
        if side == "SHORT":
            return price * (1 - slip) if leg == "entry" else price * (1 + slip)
        return price

    @staticmethod
    def _as_list(value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    def _matches_event_profile(self, event: Event, profile: str | None) -> bool:
        profile_key = (profile or "").strip().lower()
        if not profile_key:
            return True

        summary = event.summary or ""
        lowered = summary.lower()
        effective_event_type = resolve_event_type_for_text(event.event_type or "", summary)

        if profile_key == "earnings_only":
            if self._EARNINGS_EXCLUDE_RE.search(summary):
                return False
            if effective_event_type == "sec_earnings_release":
                return True
            if effective_event_type == "earnings_miss":
                return True
            if effective_event_type == "guidance_cut" and self._EARNINGS_KEYWORD_RE.search(summary):
                return True
            if not self._EARNINGS_KEYWORD_RE.search(summary):
                return False
            return bool(self._EARNINGS_RELEASE_RE.search(summary)) and not any(
                token in lowered for token in ("next earnings report", "earnings conference", "earnings call")
            )

        return True

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
            effective_event_type = resolve_event_type_for_text(event.event_type or "", event.summary or "")
            key = (str(event.tickers[0]).upper(), effective_event_type or "", event_ts.date())
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

    def _deduplicate_earnings_window_events(self, events: list[Event], window_hours: int = 36) -> list[Event]:
        """Keep only the first anchor event for a ticker's earnings window, skip later recaps/follow-ups."""
        deduped: list[Event] = []
        last_anchor_ts_by_ticker: dict[str, datetime] = {}
        window = timedelta(hours=max(1, window_hours))

        for event in events:
            if not event.tickers:
                deduped.append(event)
                continue

            ticker = str(event.tickers[0]).upper()
            event_ts = ensure_utc(event.event_time)
            summary = event.summary or ""
            effective_event_type = resolve_event_type_for_text(event.event_type, summary)
            is_anchor = is_earnings_window_event(effective_event_type, summary)
            is_followup = is_price_action_recap(summary)

            last_anchor_ts = last_anchor_ts_by_ticker.get(ticker)
            within_window = bool(last_anchor_ts and event_ts - last_anchor_ts <= window)
            if within_window and (is_anchor or is_followup):
                continue

            if is_anchor:
                last_anchor_ts_by_ticker[ticker] = event_ts
            deduped.append(event)

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
        regular_session_only: bool = False,
    ) -> tuple[datetime, float, str] | None:
        if stop_loss_pct <= 0 and take_profit_pct <= 0:
            return None

        bars = self._bars_between(
            session,
            ticker,
            entry_ts,
            end_ts,
            regular_session_only=regular_session_only,
        )
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

    def _first_breakout_confirmation_bar(
        self,
        session: Session,
        ticker: str,
        *,
        direction: str,
        start_ts: datetime,
        end_ts: datetime,
        lookback_min: int,
        regular_session_only: bool = False,
    ) -> Bar1m | None:
        direction_u = (direction or "").upper().strip()
        if direction_u not in {"BUY", "SHORT"}:
            return None

        window_start = ensure_utc(start_ts)
        window_end = ensure_utc(end_ts)
        if window_end <= window_start:
            return None

        bars = (
            session.execute(
                select(Bar1m)
                .where(
                    and_(
                        Bar1m.ticker == ticker,
                        Bar1m.ts >= window_start,
                        Bar1m.ts <= window_end,
                    )
                )
                .order_by(Bar1m.ts.asc())
            )
            .scalars()
            .all()
        )
        if regular_session_only:
            bars = [bar for bar in bars if self._is_regular_session_bar(bar.ts)]
        if not bars:
            return None

        lookback = max(5, min(int(lookback_min), 240))
        for candidate in bars:
            history = (
                session.execute(
                    select(Bar1m.high, Bar1m.low)
                    .where(
                        and_(
                            Bar1m.ticker == ticker,
                            Bar1m.ts < candidate.ts,
                            Bar1m.ts >= ensure_utc(candidate.ts) - timedelta(minutes=lookback + 2),
                        )
                    )
                    .order_by(Bar1m.ts.desc())
                    .limit(lookback)
                )
                .all()
            )
            if len(history) < 3:
                continue
            range_high = max(float(row[0]) for row in history)
            range_low = min(float(row[1]) for row in history)
            close_px = float(candidate.close or 0.0)
            if direction_u == "BUY" and close_px >= range_high:
                return candidate
            if direction_u == "SHORT" and close_px <= range_low:
                return candidate
        return None

    def _compute_metrics(self, initial_nav: float, equity_curve: list[dict], pnl_list: list[float]) -> dict:
        final_nav = equity_curve[-1]["equity"] if equity_curve else initial_nav
        total_return = (final_nav - initial_nav) / initial_nav if initial_nav else 0.0

        minutes = 1.0
        if len(equity_curve) >= 2:
            try:
                started_at = ensure_utc(datetime.fromisoformat(str(equity_curve[0]["ts"])))
                ended_at = ensure_utc(datetime.fromisoformat(str(equity_curve[-1]["ts"])))
                minutes = max((ended_at - started_at).total_seconds() / 60.0, 1.0)
            except (TypeError, ValueError):
                minutes = float(max(1, len(equity_curve)))
        annualization_factor = (252 * 390) / minutes
        if total_return <= -1:
            annualized_return = -1.0
        else:
            annualized_log_return = math.log1p(total_return) * annualization_factor
            annualized_return = math.expm1(min(annualized_log_return, 700.0))

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

    def run(self, session: Session, params: dict | None = None, run_id: int | None = None) -> BacktestResult:
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
        regular_session_only = self._as_bool(
            params.get("regular_session_only"),
            default=self.settings.backtest_regular_session_only,
        )
        max_next_session_delay_min = int(
            params.get("max_next_session_delay_min", self.settings.backtest_max_next_session_delay_min)
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
        flow_confirmation_enabled = self._as_bool(
            params.get("flow_confirmation_enabled"),
            default=getattr(self.settings, "flow_confirmation_enabled", True),
        )
        flow_confirmation_soft_gate = self._as_bool(
            params.get("flow_confirmation_soft_gate"),
            default=getattr(self.settings, "flow_confirmation_soft_gate", True),
        )
        flow_breakout_lookback_min = int(
            params.get(
                "flow_breakout_lookback_min",
                getattr(self.settings, "live_entry_plan_breakout_lookback_min", 15),
            )
        )
        flow_breakout_lookback_min = max(5, min(flow_breakout_lookback_min, 120))
        flow_wait_valid_minutes = int(
            params.get(
                "flow_wait_valid_minutes",
                getattr(self.settings, "live_entry_plan_default_valid_minutes", 180),
            )
        )
        flow_wait_valid_minutes = max(5, min(flow_wait_valid_minutes, 1440))
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
        event_profile = str(params.get("event_profile") or "").strip().lower()
        selected_sources = sorted({
            normalize_source_name(source.strip().lower())
            for source in self._as_list(params.get("sources"))
            if source and source.strip()
        })

        if run_id is not None:
            run = session.get(BacktestRun, run_id)
            if run is None:
                run = BacktestRun(id=run_id, params=params, status="RUNNING")
                session.add(run)
            else:
                run.params = params
                run.metrics = {}
                run.equity_curve = []
                run.trade_log = []
                run.status = "RUNNING"
                run.finished_at = None
            session.flush()
            session.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run.id))
        else:
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
        if selected_sources:
            source_filtered_events: list[Event] = []
            selected_source_set = set(selected_sources)
            for event in events:
                evidence_rows = self.analysis._evidence_rows(session, event, limit=4)
                event_sources = {normalize_source_name(row.get("source")) for row in evidence_rows if row.get("source")}
                if event_sources & selected_source_set:
                    source_filtered_events.append(event)
            events = source_filtered_events
        profile_filtered = 0
        if event_profile:
            filtered_events = [event for event in events if self._matches_event_profile(event, event_profile)]
            profile_filtered = max(0, len(events) - len(filtered_events))
            events = filtered_events
        events_before_dedup = len(events)
        same_day_dedup_dropped = 0
        earnings_window_dedup_dropped = 0
        if dedup_same_day_event:
            same_day_events = self._deduplicate_same_day_events(events)
            same_day_dedup_dropped = max(0, len(events) - len(same_day_events))
            events = self._deduplicate_earnings_window_events(same_day_events)
            earnings_window_dedup_dropped = max(0, len(same_day_events) - len(events))
        dedup_dropped = same_day_dedup_dropped + earnings_window_dedup_dropped
        started = time.perf_counter()
        total_events = len(events)
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
        flow_scaled_trades = 0
        flow_scaled_up_trades = 0
        flow_scaled_down_trades = 0
        flow_wait_mode_events = 0
        flow_wait_triggered = 0
        flow_wait_expired = 0
        flow_score_sum = 0.0
        flow_score_count = 0
        flow_bucket_counts: dict[str, int] = {}

        current_day: date | None = None
        day_start_equity = equity
        day_halted = False
        processed_events = 0
        current_phase = "loading_events"

        def persist_run_progress(
            *,
            processed: int | None = None,
            trades: int | None = None,
            phase: str | None = None,
            phase_current: int | None = None,
            phase_total: int | None = None,
            phase_detail: str | None = None,
            force_commit: bool = False,
        ) -> None:
            nonlocal processed_events, current_phase
            if processed is None:
                processed = processed_events
            processed_events = processed
            if phase is not None:
                current_phase = phase
            if trades is None:
                trades = len(pnl_list)
            progress_pct = (processed / total_events * 100.0) if total_events else 100.0

            partial_metrics = dict(run.metrics or {})
            if phase_current is None:
                phase_current = int(partial_metrics.get("phase_current", 0) or 0)
            if phase_total is None:
                phase_total = int(partial_metrics.get("phase_total", 0) or 0)
            phase_pct = (
                max(0.0, min(100.0, (phase_current / phase_total) * 100.0))
                if phase_total > 0 else 0.0
            )
            partial_metrics.update(
                {
                    "progress_current": processed,
                    "progress_total": total_events,
                    "progress_pct": progress_pct,
                    "trades_so_far": trades,
                    "llm_signals_so_far": llm_signals,
                    "equity_so_far": equity,
                    "selected_sources": selected_sources,
                    "event_profile": event_profile,
                    "phase": current_phase,
                    "phase_label": self._BACKTEST_PHASE_LABELS.get(current_phase, current_phase.replace("_", " ").title()),
                    "phase_current": phase_current,
                    "phase_total": phase_total,
                    "phase_pct": phase_pct,
                    "phase_detail": phase_detail if phase_detail is not None else partial_metrics.get("phase_detail"),
                    "last_progress_at": utc_now().isoformat(),
                }
            )
            run.status = "RUNNING"
            run.metrics = partial_metrics
            session.flush()
            if force_commit:
                session.commit()

        def set_phase(
            phase: str,
            *,
            current: int = 0,
            total: int = 0,
            detail: str = "",
            force_commit: bool = True,
        ) -> None:
            persist_run_progress(
                phase=phase,
                phase_current=current,
                phase_total=total,
                phase_detail=detail,
                force_commit=force_commit,
            )
            self.logger.info(
                "回测阶段 run_id=%s phase=%s %s/%s detail=%s",
                run.id,
                phase,
                current,
                total,
                detail,
            )
            log_writeout(
                "backtest_phase",
                {
                    "run_id": run.id,
                    "phase": phase,
                    "phase_label": self._BACKTEST_PHASE_LABELS.get(phase, phase.replace("_", " ").title()),
                    "phase_current": current,
                    "phase_total": total,
                    "phase_detail": detail,
                },
            )

        set_phase("loading_events", current=1, total=1, detail="Loading candidate events")

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
                "event_profile": event_profile,
                "profile_filtered": profile_filtered,
                "use_llm": use_llm,
                "min_confidence": min_conf,
                "horizon_min": horizon_min,
                "start_date": start_date,
                "end_date": end_date,
                "sources": selected_sources,
                "entry_window_min": entry_window_min,
                "hard_stops": hard_stops,
                "risk_sizing": risk_sizing,
                "risk_per_trade_pct": risk_per_trade_pct,
                "regime_risk_adjust": regime_risk_adjust,
                "regime_bull_risk_multiplier": regime_bull_risk_multiplier,
                "regime_bear_risk_multiplier": regime_bear_risk_multiplier,
                "dedup_same_day_event": dedup_same_day_event,
                "dedup_dropped": dedup_dropped,
                "same_day_dedup_dropped": same_day_dedup_dropped,
                "earnings_window_dedup_dropped": earnings_window_dedup_dropped,
                "slippage_bps": slippage_bps,
                "daily_circuit_breaker": daily_circuit_breaker,
                "enable_term_horizon": enable_term_horizon,
                "allow_unknown_with_llm": allow_unknown_with_llm,
                "allow_next_session_entry": allow_next_session_entry,
                "regular_session_only": regular_session_only,
                "max_next_session_delay_min": max_next_session_delay_min,
                "use_tradeability_filter": use_tradeability_filter,
                "tradeability_min_score": tradeability_min_score,
                "use_event_quality_filter": use_event_quality_filter,
                "event_quality_min_score": event_quality_min_score,
                "event_quality_fail_open": event_quality_fail_open,
                "conviction_position_sizing": conviction_position_sizing,
                "flow_confirmation_enabled": flow_confirmation_enabled,
                "flow_confirmation_soft_gate": flow_confirmation_soft_gate,
                "flow_breakout_lookback_min": flow_breakout_lookback_min,
                "flow_wait_valid_minutes": flow_wait_valid_minutes,
            },
        )

        def emit_progress(idx: int) -> None:
            if idx % progress_every != 0 and idx != total_events:
                return
            elapsed = time.perf_counter() - started
            persist_run_progress(
                processed=idx,
                trades=len(pnl_list),
                phase="event_execution",
                phase_current=idx,
                phase_total=total_events,
                phase_detail=f"Processed {idx} of {total_events} events",
                force_commit=True,
            )
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
            earliest_event_by_ticker: dict[str, datetime] = {}
            for event in tradeable_events:
                if not event.tickers:
                    continue
                ticker_key = str(event.tickers[0]).upper()
                event_ts = ensure_utc(event.event_time)
                current = earliest_event_by_ticker.get(ticker_key)
                if current is None or event_ts < current:
                    earliest_event_by_ticker[ticker_key] = event_ts
            unique_tickers = list(earliest_event_by_ticker.keys())
            if unique_tickers:
                set_phase(
                    "cache_warmup",
                    current=0,
                    total=len(unique_tickers),
                    detail=f"Warming Finnhub context for {len(unique_tickers)} tickers",
                )
            self.logger.info(
                "预热Finnhub cache run_id=%s unique_tickers=%d",
                run.id, len(unique_tickers),
            )
            for i, tk in enumerate(unique_tickers):
                self.analysis._finnhub_earnings_context(tk, event_ts=earliest_event_by_ticker[tk])
                if (i + 1) % 5 == 0 or (i + 1) == len(unique_tickers):
                    persist_run_progress(
                        phase="cache_warmup",
                        phase_current=i + 1,
                        phase_total=len(unique_tickers),
                        phase_detail=f"Warmed Finnhub context for {i + 1}/{len(unique_tickers)} tickers",
                        force_commit=True,
                    )
                if (i + 1) % 10 == 0 or (i + 1) == len(unique_tickers):
                    self.logger.info("Finnhub cache预热 %d/%d", i + 1, len(unique_tickers))

            if tradeable_events:
                set_phase(
                    "llm_prefetch",
                    current=0,
                    total=len(tradeable_events),
                    detail=f"Prefetching LLM signals for {len(tradeable_events)} events",
                )
            self.logger.info(
                "并发LLM预取 run_id=%s workers=%s tradeable=%s/%s",
                run.id, llm_workers, len(tradeable_events), total_events,
            )
            llm_started = time.perf_counter()
            completed_count = 0

            supports_parallel_prefetch = session.get_bind().dialect.name != "sqlite" and llm_workers > 1
            make_session = sessionmaker(
                bind=session.get_bind(),
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
                future=True,
            )

            def _fetch_signal(ev):
                thread_session = make_session()
                try:
                    thread_event = thread_session.get(Event, ev.id)
                    if thread_event is None:
                        return ev.id, None
                    return ev.id, self.analysis.event_to_signal(
                        thread_event,
                        session=thread_session,
                        use_tradeability_filter=use_tradeability_filter,
                    )
                finally:
                    thread_session.close()

            if supports_parallel_prefetch:
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
                            persist_run_progress(
                                phase="llm_prefetch",
                                phase_current=completed_count,
                                phase_total=len(tradeable_events),
                                phase_detail=f"Prefetched {completed_count}/{len(tradeable_events)} signals",
                                force_commit=True,
                            )
                            self.logger.info(
                                "LLM预取进度 %d/%d (%.1f/s) eta=%.0fs",
                                completed_count, len(tradeable_events),
                                rate,
                                (len(tradeable_events) - completed_count) / rate if rate > 0 else 0,
                            )
            else:
                for ev in tradeable_events:
                    try:
                        eid, sig = _fetch_signal(ev)
                        signal_map[eid] = sig
                    except Exception as exc:
                        self.logger.warning("LLM预取失败 event_id=%s: %s", ev.id, exc)
                        signal_map[ev.id] = None
                    completed_count += 1
                    if completed_count % max(1, min(llm_workers, 4) * 4) == 0 or completed_count == len(tradeable_events):
                        elapsed_llm = time.perf_counter() - llm_started
                        rate = completed_count / elapsed_llm if elapsed_llm > 0 else 0
                        persist_run_progress(
                            phase="llm_prefetch",
                            phase_current=completed_count,
                            phase_total=len(tradeable_events),
                            phase_detail=f"Prefetched {completed_count}/{len(tradeable_events)} signals",
                            force_commit=True,
                        )
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

        set_phase(
            "event_execution",
            current=0,
            total=total_events,
            detail=f"Executing {total_events} events",
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
            flow_score: int | None = None
            flow_bucket: str | None = None
            flow_position_multiplier = 1.0
            flow_wait_mode = False

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

            flow_direction = "BUY" if str(action).upper() == "BUY" else "SHORT"
            if flow_confirmation_enabled and flow_direction in {"BUY", "SHORT"}:
                flow = self.capital_confirmation.evaluate(
                    session,
                    ticker=ticker,
                    direction=flow_direction,
                    as_of=event_ts,
                )
                flow_score = int(flow.get("flow_score", 50) or 50)
                flow_bucket = str(flow.get("flow_bucket", "LOW") or "LOW").upper()
                flow_position_multiplier = max(0.0, float(flow.get("position_multiplier", 1.0) or 1.0))
                flow_score_sum += float(flow_score)
                flow_score_count += 1
                flow_bucket_counts[flow_bucket] = flow_bucket_counts.get(flow_bucket, 0) + 1
                if flow_confirmation_soft_gate and flow_score < 40:
                    flow_wait_mode = True
                    flow_wait_mode_events += 1

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

            entry_deadline_min = entry_window_min
            if flow_wait_mode:
                entry_deadline_min = max(entry_deadline_min, flow_wait_valid_minutes)
                entry_bar = self._first_breakout_confirmation_bar(
                    session,
                    ticker,
                    direction=flow_direction,
                    start_ts=event_ts + timedelta(minutes=1),
                    end_ts=event_ts + timedelta(minutes=entry_deadline_min),
                    lookback_min=flow_breakout_lookback_min,
                    regular_session_only=regular_session_only,
                )
                if entry_bar is None:
                    flow_wait_expired += 1
                    emit_progress(idx)
                    continue
                flow_wait_triggered += 1
            else:
                entry_bar = self._bar_at_or_after(
                    session,
                    ticker,
                    event_ts + timedelta(minutes=1),
                    regular_session_only=regular_session_only,
                )
            # Skip if no bar within configured entry window of event.
            if not entry_bar:
                emit_progress(idx)
                continue
            if ensure_utc(entry_bar.ts) > event_ts + timedelta(minutes=entry_deadline_min):
                delay_min = (ensure_utc(entry_bar.ts) - event_ts).total_seconds() / 60.0
                allow_next_session = (
                    allow_next_session_entry
                    and self._is_regular_session_open_bar(ensure_utc(entry_bar.ts))
                    and delay_min <= max_next_session_delay_min
                )
                if not allow_next_session:
                    entry_late_skipped += 1
                    emit_progress(idx)
                    continue
                next_session_entry_used += 1
            planned_exit_ts = ensure_utc(entry_bar.ts) + timedelta(minutes=local_horizon_min)
            planned_exit_bar = self._bar_at_or_after(
                session,
                ticker,
                planned_exit_ts,
                regular_session_only=regular_session_only,
            )
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
            if flow_confirmation_enabled and flow_confirmation_soft_gate and qty > 0:
                multiplier = max(0.0, float(flow_position_multiplier or 1.0))
                if abs(multiplier - 1.0) > 1e-9:
                    qty *= multiplier
                    # Hard clamp after flow scaling so add-on logic never exceeds single-name cap.
                    max_cap_qty = (equity * self.settings.max_position_pct) / max(entry_px, 0.01)
                    qty = min(qty, max_cap_qty)
                    flow_scaled_trades += 1
                    if multiplier > 1.0:
                        flow_scaled_up_trades += 1
                    else:
                        flow_scaled_down_trades += 1
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
                    regular_session_only=regular_session_only,
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
                "flow_score": flow_score,
                "flow_bucket": flow_bucket,
                "flow_position_multiplier": flow_position_multiplier,
                "flow_wait_mode": flow_wait_mode,
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
            evidence_rows = self.analysis._evidence_rows(session, event, limit=1)
            if evidence_rows:
                source = normalize_source_name(str(evidence_rows[0].get("source") or ""))
                if source:
                    source_attr[source] = source_attr.get(source, 0.0) + pnl

            emit_progress(idx)

        set_phase("finalizing", current=1, total=1, detail="Computing metrics and writing results")

        metrics = self._compute_metrics(self.settings.initial_nav, equity_curve, pnl_list)
        metrics["events_considered"] = len(events)
        metrics["event_profile"] = event_profile
        metrics["selected_sources"] = selected_sources
        metrics["profile_filtered"] = profile_filtered
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
        metrics["same_day_dedup_dropped"] = same_day_dedup_dropped
        metrics["earnings_window_dedup_dropped"] = earnings_window_dedup_dropped
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
        metrics["regular_session_only"] = regular_session_only
        metrics["max_next_session_delay_min"] = max_next_session_delay_min
        metrics["conviction_position_sizing"] = conviction_position_sizing
        metrics["conviction_min_risk_multiplier"] = self.settings.backtest_conviction_min_risk_multiplier
        metrics["conviction_max_risk_multiplier"] = self.settings.backtest_conviction_max_risk_multiplier
        metrics["conviction_position_floor"] = self.settings.backtest_conviction_position_floor
        metrics["flow_confirmation_enabled"] = flow_confirmation_enabled
        metrics["flow_confirmation_soft_gate"] = flow_confirmation_soft_gate
        metrics["flow_breakout_lookback_min"] = flow_breakout_lookback_min
        metrics["flow_wait_valid_minutes"] = flow_wait_valid_minutes
        metrics["flow_scaled_trades"] = flow_scaled_trades
        metrics["flow_scaled_up_trades"] = flow_scaled_up_trades
        metrics["flow_scaled_down_trades"] = flow_scaled_down_trades
        metrics["flow_wait_mode_events"] = flow_wait_mode_events
        metrics["flow_wait_triggered"] = flow_wait_triggered
        metrics["flow_wait_expired"] = flow_wait_expired
        metrics["flow_bucket_counts"] = flow_bucket_counts
        metrics["avg_flow_score"] = (flow_score_sum / flow_score_count) if flow_score_count else None
        metrics["entry_late_skipped"] = entry_late_skipped
        metrics["next_session_entry_used"] = next_session_entry_used
        metrics["progress_current"] = total_events
        metrics["progress_total"] = total_events
        metrics["progress_pct"] = 100.0
        metrics["phase"] = "completed"
        metrics["phase_label"] = self._BACKTEST_PHASE_LABELS["completed"]
        metrics["phase_current"] = total_events if total_events > 0 else 1
        metrics["phase_total"] = total_events if total_events > 0 else 1
        metrics["phase_pct"] = 100.0
        metrics["phase_detail"] = "Backtest completed"
        metrics["last_progress_at"] = utc_now().isoformat()

        run.metrics = metrics
        run.equity_curve = equity_curve
        run.trade_log = trade_log
        run.status = "DONE"
        run.finished_at = utc_now()
        session.flush()
        session.commit()

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
