"""Agent-mode backtest engine with strict temporal isolation.

Runs the full agent graph (MacroAnalyst → News → Fundamentals → Technicals →
RiskManager → PortfolioManager) day-by-day over historical data, ensuring no
look-ahead bias: all data queries are bounded by the simulation timestamp.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from heapq import heappop, heappush
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select, func
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.agent_graph.graph import AgentGraph
from app.analysis.service import AnalysisService
from app.analysis.taxonomy import normalize_source_name
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.core.utils import ensure_utc
from app.db.models import Bar1m, Event, EventEvidence

logger = get_app_logger()

_NY = ZoneInfo("America/New_York")
_MARKET_CLOSE = dt_time(16, 0)
_MARKET_OPEN = dt_time(9, 30)
_print_lock = threading.Lock()

# ── Sector map for concentration limiting ────────────────────────────────────
_TICKER_SECTOR: dict[str, str] = {
    # Tech
    "AAPL": "tech", "NVDA": "tech", "MSFT": "tech", "META": "tech",
    "AMZN": "tech", "GOOGL": "tech", "GOOG": "tech", "TSLA": "tech",
    "ORCL": "tech", "ADBE": "tech", "CRM": "tech", "AMD": "tech",
    "INTC": "tech", "QCOM": "tech", "TXN": "tech", "AVGO": "tech",
    "NFLX": "tech", "PYPL": "tech", "EBAY": "tech", "NOW": "tech",
    # Financials
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "MS": "financials", "WFC": "financials", "BRK.B": "financials",
    "V": "financials", "MA": "financials", "AXP": "financials",
    "BLK": "financials", "C": "financials", "USB": "financials",
    "PNC": "financials", "TFC": "financials", "COF": "financials",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy", "EOG": "energy",
    "SLB": "energy", "PSX": "energy", "MPC": "energy", "VLO": "energy",
    # Healthcare
    "JNJ": "healthcare", "PFE": "healthcare", "UNH": "healthcare",
    "ABT": "healthcare", "MRK": "healthcare", "LLY": "healthcare",
    "ABBV": "healthcare", "BMY": "healthcare", "AMGN": "healthcare",
    "MDT": "healthcare", "TMO": "healthcare", "DHR": "healthcare",
    # Consumer
    "WMT": "consumer", "HD": "consumer", "COST": "consumer",
    "MCD": "consumer", "SBUX": "consumer", "NKE": "consumer",
    "LOW": "consumer", "TGT": "consumer", "DIS": "consumer",
    "CMCSA": "consumer", "F": "consumer", "GM": "consumer",
    # Industrials
    "HON": "industrials", "CAT": "industrials", "BA": "industrials",
    "GE": "industrials", "RTX": "industrials", "LMT": "industrials",
    "GD": "industrials", "UPS": "industrials", "FDX": "industrials",
}
_MAX_SECTOR_POSITIONS = 2  # max open positions in the same sector


def _ts() -> str:
    """Compact timestamp for progress output."""
    return datetime.now().strftime("%H:%M:%S")


def _safe_print(*args, **kwargs):
    """Thread-safe print."""
    with _print_lock:
        print(*args, **kwargs)
        sys.stdout.flush()


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BTPosition:
    ticker: str
    shares: float
    avg_entry: float
    side: str  # LONG or SHORT
    entry_date: date
    entry_ts: datetime | None = None
    stop_loss_pct: float = 0.03


@dataclass
class BTTrade:
    date: date
    ticker: str
    side: str  # BUY, SELL, SHORT, COVER
    shares: float
    price: float
    notional: float
    reason: str
    ts: datetime | None = None


@dataclass
class BTDecision:
    date: date
    ticker: str
    action: str
    position_pct: float
    reasoning: str
    agent_signals: dict
    ts: datetime | None = None


@dataclass
class BTCandidateEvent:
    ticker: str
    as_of: datetime
    event: Event
    trigger_event: dict[str, Any]


@dataclass
class BTScheduledEntry:
    ticker: str
    action: str
    pos_pct: float
    exec_ts: datetime
    exec_price: float
    decision_ts: datetime
    trigger_event_id: int
    decision: BTDecision
    stop_loss_pct: float


@dataclass
class BTScheduledExit:
    ticker: str
    exec_ts: datetime
    price: float
    reason: str


@dataclass
class BTPortfolio:
    cash: float
    positions: dict[str, BTPosition] = field(default_factory=dict)

    def equity(self, prices: dict[str, float]) -> float:
        eq = self.cash
        for ticker, pos in self.positions.items():
            price = prices.get(ticker, pos.avg_entry)
            if pos.side == "LONG":
                eq += pos.shares * price
            else:
                # Short: cash already has proceeds from short sale.
                # We owe shares at current market price to close.
                eq -= pos.shares * price
        return eq

    def position_value(self, ticker: str, price: float) -> float:
        pos = self.positions.get(ticker)
        if not pos:
            return 0.0
        value = pos.shares * price
        return value if pos.side == "LONG" else -value


@dataclass
class AgentBacktestResult:
    start_date: date
    end_date: date
    tickers: list[str]
    initial_capital: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    stop_losses: int
    equity_curve: list[dict]
    trades: list[BTTrade]
    decisions: list[BTDecision]
    errors: list[str]


# ── Engine ────────────────────────────────────────────────────────────────────

class AgentBacktestEngine:
    """Backtest the agent pipeline over historical data."""

    def __init__(
        self,
        settings: Settings,
        *,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.progress_callback = progress_callback
        self.analysis = AnalysisService(settings)

    def run(self, session: Session, params: dict | None = None) -> AgentBacktestResult:
        """Execute an agent-mode backtest.

        Params:
            tickers: list of ticker symbols (default: ["AAPL", "NVDA", "JPM", "XOM", "AMZN"])
            start_date: "YYYY-MM-DD" (default: "2026-02-02")
            end_date: "YYYY-MM-DD" (default: "2026-02-27")
            initial_capital: float (default: 100000)
            decision_frequency: int trading days between decisions (default: 3)
            agent_entry_timing: "event_time" or "daily_next_open" (default: "event_time")
            max_position_pct: float max per-ticker position (default: 0.15)
            slippage_pct: float slippage per trade (default: 0.0005 = 0.05%)
            stop_loss_pct: float default stop-loss (default: 0.07 = 7%)
        """
        p = params or {}
        tickers = p.get("tickers", ["AAPL", "NVDA", "JPM", "XOM", "AMZN"])
        start = date.fromisoformat(p.get("start_date", "2026-02-02"))
        end = date.fromisoformat(p.get("end_date", "2026-02-27"))
        initial_capital = float(p.get("initial_capital", 100_000))
        freq = int(p.get("decision_frequency", 3))
        max_pos_pct = float(p.get("max_position_pct", 0.15))
        slippage_pct = float(p.get("slippage_pct", 0.0005))
        stop_loss_pct = float(p.get("stop_loss_pct", 0.07))
        intraday_flatten = bool(p.get("intraday_flatten", getattr(self.settings, "backtest_intraday_flatten", False)))
        use_event_quality_filter = bool(
            p.get("use_event_quality_filter", getattr(self.settings, "backtest_use_event_quality_filter", True))
        )
        event_quality_min_score = int(
            p.get("event_quality_min_score", getattr(self.settings, "backtest_event_quality_min_score", 55))
        )
        event_quality_fail_open = bool(
            p.get("event_quality_fail_open", getattr(self.settings, "backtest_event_quality_fail_open", False))
        )
        agent_entry_timing = str(p.get("agent_entry_timing") or "event_time").strip().lower()
        if agent_entry_timing not in {"event_time", "daily_next_open"}:
            agent_entry_timing = "event_time"
        allowed_sources = sorted(
            {
                normalize_source_name(str(source).strip().lower())
                for source in (p.get("sources") or [])
                if str(source).strip()
            }
            - {"yahoo", "yahoo_finance"}
        )

        if agent_entry_timing == "event_time":
            return self._run_event_time_mode(
                session,
                tickers=tickers,
                start=start,
                end=end,
                initial_capital=initial_capital,
                max_pos_pct=max_pos_pct,
                slippage_pct=slippage_pct,
                stop_loss_pct=stop_loss_pct,
                intraday_flatten=intraday_flatten,
                use_event_quality_filter=use_event_quality_filter,
                event_quality_min_score=event_quality_min_score,
                event_quality_fail_open=event_quality_fail_open,
                allowed_sources=allowed_sources,
            )

        # Build list of trading days from bar data
        trading_days = self._get_trading_days(session, start, end, tickers[0])
        if not trading_days:
            return AgentBacktestResult(
                start_date=start, end_date=end, tickers=tickers,
                initial_capital=initial_capital, final_equity=initial_capital,
                total_return_pct=0.0, max_drawdown_pct=0.0,
                total_trades=0, winning_trades=0, losing_trades=0,
                equity_curve=[], trades=[], decisions=[], errors=["No trading days found"],
            )

        decision_days = trading_days[::freq]
        total_decisions = len(decision_days) * len(tickers)
        logger.info(
            "[agent_backtest] %d trading days, %d decision days, %d tickers",
            len(trading_days), len(decision_days), len(tickers),
        )
        print(f"\n{'='*60}")
        print(f"[{_ts()}] 🚀 AGENT BACKTEST START")
        print(f"  Tickers: {', '.join(tickers)}")
        print(f"  Period: {start} → {end} ({len(trading_days)} trading days)")
        print(f"  Decisions every {freq} days → {len(decision_days)} decision points")
        print(f"  Capital: ${initial_capital:,.0f}")
        print(f"{'='*60}")
        sys.stdout.flush()

        portfolio = BTPortfolio(cash=initial_capital)
        equity_curve: list[dict] = []
        all_trades: list[BTTrade] = []
        all_decisions: list[BTDecision] = []
        errors: list[str] = []
        make_session = sessionmaker(
            bind=session.get_bind(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
        graph = AgentGraph(self.settings, session_factory=make_session)

        # Per-ticker consecutive loss tracking
        ticker_loss_streak: dict[str, int] = {t: 0 for t in tickers}
        ticker_last_side: dict[str, str | None] = {}

        # Direction inertia: track when each ticker last changed direction
        # Key: ticker, Value: decision_day index when position was opened/reversed
        ticker_entry_decision_idx: dict[str, int] = {}
        _INERTIA_CYCLES = 3  # must hold at least 3 decision cycles before reversing

        peak_equity = initial_capital
        max_drawdown = 0.0
        processed_decisions = 0
        pending_entries: dict[date, list[dict[str, Any]]] = {}

        def _publish_progress(detail: str) -> None:
            if not self.progress_callback:
                return
            self.progress_callback(processed_decisions, total_decisions, detail)

        for day_idx, day in enumerate(trading_days):
            is_decision_day = day in decision_days

            if intraday_flatten:
                scheduled_entries = pending_entries.pop(day, [])
                if scheduled_entries:
                    open_prices = {
                        ticker_open: self._get_price(session, ticker_open, day, "open")
                        for ticker_open in tickers
                    }
                    for entry in scheduled_entries:
                        ticker = entry["ticker"]
                        action = entry["action"]
                        pos_pct = float(entry["pos_pct"])
                        decision_idx = int(entry["decision_idx"])
                        trades_before = len(all_trades)
                        open_price = open_prices.get(ticker)
                        if not open_price or open_price <= 0:
                            continue
                        exec_price = open_price * (1 + slippage_pct) if action == "BUY" else open_price * (1 - slippage_pct)
                        target_pct = min(pos_pct, max_pos_pct)
                        equity = portfolio.equity({k: float(v) for k, v in open_prices.items() if v and v > 0})
                        atr_stop = self._compute_atr_stop(
                            session, ticker, day, default_stop=stop_loss_pct
                        )
                        self._execute_decision(
                            portfolio,
                            ticker,
                            action,
                            target_pct,
                            equity,
                            exec_price,
                            day,
                            all_trades,
                            stop_loss_pct=atr_stop,
                        )
                        if len(all_trades) > trades_before:
                            t = all_trades[-1]
                            print(f"    💰 TRADE: {t.side} {t.shares:.2f} {t.ticker} @ ${t.price:.2f} (${t.notional:,.0f}) [stop={atr_stop:.1%}]")
                            sys.stdout.flush()
                            ticker_last_side[ticker] = action
                            ticker_entry_decision_idx[ticker] = decision_idx

            if is_decision_day:
                # Run agent graph for each ticker — parallel across tickers
                as_of = datetime.combine(day, _MARKET_CLOSE, tzinfo=_NY).astimezone(timezone.utc)
                decision_idx = decision_days.index(day) + 1
                print(f"\n[{_ts()}] 📊 Decision Day {decision_idx}/{len(decision_days)}: {day}")
                sys.stdout.flush()
                _publish_progress(f"Decision day {decision_idx}/{len(decision_days)} started for {len(tickers)} tickers")

                # Pre-compute shared state for all tickers
                close_prices = self._get_close_prices(session, tickers, day)
                current_equity = portfolio.equity(close_prices)
                dd_pct = max_drawdown

                # Build portfolio positions snapshot for concentration checks
                portfolio_positions = {
                    t: {"side": p.side, "shares": p.shares, "entry": p.avg_entry}
                    for t, p in portfolio.positions.items()
                }

                def _run_ticker_agents(ticker: str) -> dict:
                    """Run agent graph for a single ticker (thread-safe)."""
                    t0 = time.time()
                    _safe_print(f"  [{_ts()}] 🤖 Running agents for {ticker}...", end="")
                    local_session = make_session()
                    try:
                        trigger_event = self._find_trigger_event(
                            local_session,
                            ticker=ticker,
                            as_of=as_of,
                            allowed_sources=allowed_sources,
                            use_event_quality_filter=use_event_quality_filter,
                            event_quality_min_score=event_quality_min_score,
                            event_quality_fail_open=event_quality_fail_open,
                        )
                        if trigger_event is None:
                            _safe_print(" → HOLD 0% (strong_news_gate)")
                            return {
                                "ticker": ticker,
                                "action": "HOLD",
                                "pos_pct": 0.0,
                                "reasoning": "[strong_news_gate] no strong ticker-specific tradeable event",
                                "signals": {},
                                "state": {"final_action": "HOLD", "final_position_pct": 0.0, "agent_signals": {}},
                            }

                        pos = portfolio.positions.get(ticker)
                        pos_value = portfolio.position_value(ticker, close_prices.get(ticker, 0))
                        pos_pct_current = pos_value / max(current_equity, 1)
                        current_side = pos.side if pos else None

                        daily_pnl = self._compute_daily_realized_pnl(
                            all_trades,
                            target_day=day,
                            ticker=ticker,
                        )

                        bt_context = {
                            "portfolio_state": {
                                "position_pct": pos_pct_current,
                                "daily_pnl": daily_pnl,
                                "equity": current_equity,
                                "current_side": current_side,
                                "drawdown_pct": dd_pct,
                            },
                            "current_position": {
                                "side": current_side,
                                "shares": pos.shares if pos else 0,
                                "entry_price": pos.avg_entry if pos else 0,
                                "entry_date": str(pos.entry_date) if pos else None,
                            },
                            "portfolio_positions": portfolio_positions,
                            "ticker_loss_streak": ticker_loss_streak,
                            "trigger_event_id": trigger_event["id"],
                            "trigger_event": trigger_event,
                        }
                        if allowed_sources:
                            bt_context["allowed_sources"] = allowed_sources
                        state = graph.run(local_session, ticker, context=bt_context, as_of=as_of)
                    finally:
                        local_session.close()

                    elapsed = time.time() - t0

                    action = state.get("final_action", "HOLD")
                    pos_pct = float(state.get("final_position_pct", 0.0))
                    reasoning = state.get("final_reasoning", "")[:300]
                    signals = {
                        k: v.get("signal", "?") if isinstance(v, dict) else "?"
                        for k, v in state.get("agent_signals", {}).items()
                    }

                    # When portfolio_manager times out (NO_SIGNAL), fall back to
                    # risk_manager's approved direction if it exists
                    if action == "NO_SIGNAL":
                        risk_sig = state.get("agent_signals", {}).get("risk_manager", {})
                        if isinstance(risk_sig, dict) and risk_sig.get("metadata", {}).get("approved"):
                            rm_dir = risk_sig.get("signal", "HOLD")
                            rm_pct = risk_sig.get("metadata", {}).get("max_position_pct", 0.07)
                            if rm_dir in ("BUY", "SHORT"):
                                action = rm_dir
                                pos_pct = rm_pct
                                reasoning = f"[fallback from risk_manager] {risk_sig.get('reasoning','')}"
                        else:
                            action = "HOLD"
                            pos_pct = 0.0

                    signal_str = " ".join(f"{k[:4]}={v}" for k, v in signals.items())
                    _safe_print(f" → {action} {pos_pct:.0%} ({elapsed:.0f}s) [{signal_str}]")

                    return {
                        "ticker": ticker, "action": action, "pos_pct": pos_pct,
                        "reasoning": reasoning, "signals": signals, "state": state,
                    }

                # Run all tickers in parallel (limit concurrency to avoid DB pool exhaustion)
                ticker_results = []
                with ThreadPoolExecutor(max_workers=min(len(tickers), 3)) as pool:
                    futures = {pool.submit(_run_ticker_agents, t): t for t in tickers}
                    for future in as_completed(futures):
                        tk = futures[future]
                        try:
                            result = future.result(timeout=300)
                            ticker_results.append(result)
                        except Exception as exc:
                            err = f"Day {day} {tk}: {exc}"
                            logger.warning("[agent_backtest] %s", err)
                            errors.append(err)
                        finally:
                            processed_decisions += 1
                            _publish_progress(
                                f"Processed {processed_decisions}/{total_decisions} agent decisions "
                                f"(day {decision_idx}/{len(decision_days)} · {tk})"
                            )

                # Execute trades sequentially (order matters for cash management)
                for result in sorted(ticker_results, key=lambda r: tickers.index(r["ticker"])):
                    ticker = result["ticker"]
                    action = result["action"]
                    pos_pct = result["pos_pct"]

                    all_decisions.append(BTDecision(
                        date=day, ticker=ticker, action=action,
                        position_pct=pos_pct, reasoning=result["reasoning"],
                        agent_signals=result["signals"],
                    ))

                    next_day = self._next_trading_day(trading_days, day)
                    trades_before = len(all_trades)

                    # Direction inertia: block reversals within _INERTIA_CYCLES
                    pos = portfolio.positions.get(ticker)
                    if pos and action in ("BUY", "SHORT"):
                        is_reversal = (pos.side == "LONG" and action == "SHORT") or (pos.side == "SHORT" and action == "BUY")
                        if is_reversal:
                            entry_idx = ticker_entry_decision_idx.get(ticker, 0)
                            held_cycles = decision_idx - entry_idx
                            if held_cycles < _INERTIA_CYCLES:
                                print(f"    🔒 INERTIA: {ticker} held {held_cycles}/{_INERTIA_CYCLES} cycles, blocking {pos.side}→{action}")
                                sys.stdout.flush()
                                action = "HOLD"  # override to HOLD

                    if next_day and action in ("BUY", "SHORT"):
                        # Sector concentration check: max _MAX_SECTOR_POSITIONS per sector
                        ticker_sector = _TICKER_SECTOR.get(ticker, "other")
                        sector_positions = [
                            t for t, p in portfolio.positions.items()
                            if t != ticker and _TICKER_SECTOR.get(t, "other") == ticker_sector
                        ]
                        if len(sector_positions) >= _MAX_SECTOR_POSITIONS and ticker not in portfolio.positions:
                            print(f"    🏭 SECTOR LIMIT: {ticker} ({ticker_sector}) blocked — already {len(sector_positions)} in sector ({', '.join(sector_positions)})")
                            sys.stdout.flush()
                        else:
                            if intraday_flatten:
                                pending_entries.setdefault(next_day, []).append(
                                    {
                                        "ticker": ticker,
                                        "action": action,
                                        "pos_pct": pos_pct,
                                        "decision_idx": decision_idx,
                                    }
                                )
                                print(f"    🗓️  SCHEDULED: {action} {ticker} for {next_day} open")
                                sys.stdout.flush()
                                continue
                            open_price = self._get_price(session, ticker, next_day, "open")
                            if open_price and open_price > 0:
                                exec_price = open_price * (1 + slippage_pct) if action == "BUY" else open_price * (1 - slippage_pct)
                                target_pct = min(pos_pct, max_pos_pct)
                                equity = portfolio.equity(
                                    self._get_close_prices(session, tickers, day)
                                )
                                # ATR-based adaptive stop-loss per ticker
                                atr_stop = self._compute_atr_stop(
                                    session, ticker, next_day, default_stop=stop_loss_pct
                                )
                                self._execute_decision(
                                    portfolio, ticker, action, target_pct,
                                    equity, exec_price, next_day, all_trades,
                                    stop_loss_pct=atr_stop,
                                )
                                if len(all_trades) > trades_before:
                                    t = all_trades[-1]
                                    print(f"    💰 TRADE: {t.side} {t.shares:.2f} {t.ticker} @ ${t.price:.2f} (${t.notional:,.0f}) [stop={atr_stop:.1%}]")
                                    sys.stdout.flush()
                                    ticker_last_side[ticker] = action
                                    ticker_entry_decision_idx[ticker] = decision_idx
                    elif action == "SELL" and ticker in portfolio.positions:
                        open_price = self._get_price(session, ticker, next_day, "open") if next_day else None
                        if open_price and open_price > 0:
                            # Track P&L before closing
                            pos = portfolio.positions.get(ticker)
                            if pos and pos.side == "LONG":
                                trade_pnl = (open_price - pos.avg_entry) * pos.shares
                                self._update_loss_streak(ticker_loss_streak, ticker, trade_pnl)

                            exec_price = open_price * (1 - slippage_pct)
                            self._close_position(portfolio, ticker, exec_price, next_day, all_trades, "agent_sell")
                            t = all_trades[-1]
                            print(f"    💰 TRADE: {t.side} {t.shares:.2f} {t.ticker} @ ${t.price:.2f} (${t.notional:,.0f})")
                            sys.stdout.flush()
                    elif action == "HOLD" and ticker in portfolio.positions:
                        pos = portfolio.positions[ticker]
                        if pos.shares > 0 and next_day:
                            open_price = self._get_price(session, ticker, next_day, "open")
                            if open_price and open_price > 0:
                                if pos.side == "LONG":
                                    unrealized_pnl = (open_price - pos.avg_entry) * pos.shares
                                else:
                                    unrealized_pnl = (pos.avg_entry - open_price) * pos.shares
                                status = "WINNER" if unrealized_pnl >= 0 else "LOSER"
                                print(f"    {'✅' if unrealized_pnl >= 0 else '📌'} HOLD {status}: {ticker} {pos.side} unrealized P&L ${unrealized_pnl:+,.0f}")
                                sys.stdout.flush()
                    elif action == "HOLD":
                        pass

            # ── Stop-loss check on every day ──
            close_prices = self._get_close_prices(session, tickers, day)
            for ticker_sl in list(portfolio.positions.keys()):
                pos = portfolio.positions.get(ticker_sl)
                if not pos:
                    continue
                sl_pct = getattr(pos, "stop_loss_pct", stop_loss_pct)
                price = close_prices.get(ticker_sl, 0)
                if price <= 0:
                    continue
                if pos.side == "LONG" and price <= pos.avg_entry * (1 - sl_pct):
                    exec_price = price * (1 - slippage_pct)
                    print(f"  [{_ts()}] 🛑 STOP-LOSS {ticker_sl}: price ${price:.2f} < entry ${pos.avg_entry:.2f} - {sl_pct:.1%}")
                    # Track loss
                    self._update_loss_streak(ticker_loss_streak, ticker_sl, -1)
                    self._close_position(portfolio, ticker_sl, exec_price, day, all_trades, "stop_loss")
                    sys.stdout.flush()
                elif pos.side == "SHORT" and price >= pos.avg_entry * (1 + sl_pct):
                    exec_price = price * (1 + slippage_pct)
                    print(f"  [{_ts()}] 🛑 STOP-LOSS {ticker_sl}: price ${price:.2f} > entry ${pos.avg_entry:.2f} + {sl_pct:.1%}")
                    # Track loss
                    self._update_loss_streak(ticker_loss_streak, ticker_sl, -1)
                    self._close_position(portfolio, ticker_sl, exec_price, day, all_trades, "stop_loss")
                    sys.stdout.flush()

            if intraday_flatten:
                for ticker_flat in list(portfolio.positions.keys()):
                    pos = portfolio.positions.get(ticker_flat)
                    close_price = close_prices.get(ticker_flat, 0)
                    if not pos or close_price <= 0:
                        continue
                    exec_price = close_price * (1 - slippage_pct) if pos.side == "LONG" else close_price * (1 + slippage_pct)
                    self._close_position(portfolio, ticker_flat, exec_price, day, all_trades, "intraday_flatten")

            # Mark-to-market at close
            equity = portfolio.equity(close_prices)
            equity_curve.append({
                "date": str(day),
                "equity": round(equity, 2),
                "cash": round(portfolio.cash, 2),
                "positions": {
                    t: {"shares": p.shares, "side": p.side, "entry": p.avg_entry}
                    for t, p in portfolio.positions.items()
                },
            })

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity
            if dd > max_drawdown:
                max_drawdown = dd

            if day_idx % 5 == 0 or day == trading_days[-1] or is_decision_day:
                pos_str = ", ".join(
                    f"{t}:{p.side[0]}{p.shares:.0f}"
                    for t, p in portfolio.positions.items()
                ) or "none"
                logger.info(
                    "[agent_backtest] %s: Equity=$%s  Cash=$%s  Positions=%d",
                    day,
                    format(equity, ",.2f"),
                    format(portfolio.cash, ",.2f"),
                    len(portfolio.positions),
                )
                dd_pct = max_drawdown * 100
                print(f"  [{_ts()}] 📈 {day}: Equity=${equity:,.0f} | Cash=${portfolio.cash:,.0f} | DD={dd_pct:.1f}% | Pos=[{pos_str}]")
                sys.stdout.flush()

        # Close all positions at end
        final_day = trading_days[-1]
        close_prices = self._get_close_prices(session, tickers, final_day)
        print(f"\n[{_ts()}] 🔒 Closing all positions at {final_day}...")
        sys.stdout.flush()
        for ticker in list(portfolio.positions.keys()):
            price = close_prices.get(ticker, 0)
            if price > 0:
                self._close_position(portfolio, ticker, price, final_day, all_trades, "backtest_end")
                print(f"    📤 CLOSE {ticker} @ ${price:.2f}")
                sys.stdout.flush()

        final_equity = portfolio.cash
        total_return = (final_equity / initial_capital - 1) * 100

        # Count winners/losers from round-trip trades
        trade_pnls = self._compute_trade_pnls(all_trades)
        winning = sum(1 for pnl in trade_pnls if pnl > 0)
        losing = sum(1 for pnl in trade_pnls if pnl < 0)
        stop_loss_count = sum(1 for t in all_trades if t.reason == "stop_loss")

        print(f"\n{'='*60}")
        print(f"[{_ts()}] ✅ BACKTEST COMPLETE")
        print(f"  Return: {total_return:+.2f}%  (${initial_capital:,.0f} → ${final_equity:,.0f})")
        print(f"  Max Drawdown: {max_drawdown * 100:.2f}%")
        print(f"  Trades: {len(all_trades)} ({winning}W / {losing}L)")
        if stop_loss_count:
            print(f"  Stop-losses triggered: {stop_loss_count}")
        streaks = {t: s for t, s in ticker_loss_streak.items() if s > 0}
        if streaks:
            print(f"  Loss streaks: {streaks}")
        print(f"  Slippage: {slippage_pct:.2%} per trade")
        print(f"  Decisions: {len(all_decisions)}")
        print(f"{'='*60}\n")
        sys.stdout.flush()

        return AgentBacktestResult(
            start_date=start, end_date=end, tickers=tickers,
            initial_capital=initial_capital,
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return, 2),
            max_drawdown_pct=round(max_drawdown * 100, 2),
            total_trades=len(all_trades),
            winning_trades=winning, losing_trades=losing,
            stop_losses=stop_loss_count,
            equity_curve=equity_curve, trades=all_trades,
            decisions=all_decisions, errors=errors,
        )

    def _run_event_time_mode(
        self,
        session: Session,
        *,
        tickers: list[str],
        start: date,
        end: date,
        initial_capital: float,
        max_pos_pct: float,
        slippage_pct: float,
        stop_loss_pct: float,
        intraday_flatten: bool,
        use_event_quality_filter: bool,
        event_quality_min_score: int,
        event_quality_fail_open: bool,
        allowed_sources: list[str],
    ) -> AgentBacktestResult:
        trading_days = self._get_trading_days(session, start, end, tickers[0])
        if not trading_days:
            return AgentBacktestResult(
                start_date=start, end_date=end, tickers=tickers,
                initial_capital=initial_capital, final_equity=initial_capital,
                total_return_pct=0.0, max_drawdown_pct=0.0,
                total_trades=0, winning_trades=0, losing_trades=0,
                stop_losses=0,
                equity_curve=[],
                trades=[],
                decisions=[],
                errors=["No trading days found"],
            )

        candidates = self._load_event_time_candidates(
            session,
            tickers=tickers,
            start=start,
            end=end,
            allowed_sources=allowed_sources,
            use_event_quality_filter=use_event_quality_filter,
            event_quality_min_score=event_quality_min_score,
            event_quality_fail_open=event_quality_fail_open,
        )
        total_decisions = max(1, len(candidates))
        logger.info(
            "[agent_backtest] event-time mode %d candidate events, %d tickers",
            len(candidates),
            len(tickers),
        )
        print(f"\n{'='*60}")
        print(f"[{_ts()}] 🚀 AGENT BACKTEST START")
        print(f"  Mode: event_time")
        print(f"  Tickers: {', '.join(tickers)}")
        print(f"  Period: {start} → {end} ({len(trading_days)} trading days)")
        print(f"  Candidate events: {len(candidates)}")
        print(f"  Capital: ${initial_capital:,.0f}")
        print(f"{'='*60}")
        sys.stdout.flush()

        portfolio = BTPortfolio(cash=initial_capital)
        all_trades: list[BTTrade] = []
        all_decisions: list[BTDecision] = []
        equity_curve: list[dict[str, Any]] = []
        errors: list[str] = []
        make_session = sessionmaker(
            bind=session.get_bind(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
        graph = AgentGraph(self.settings, session_factory=make_session)

        ticker_loss_streak: dict[str, int] = {t: 0 for t in tickers}
        processed_decisions = 0
        peak_equity = initial_capital
        max_drawdown = 0.0
        pending_entries: list[tuple[datetime, int, BTScheduledEntry]] = []
        pending_exits: list[tuple[datetime, int, BTScheduledExit]] = []
        schedule_order = 0
        used_session_keys: set[tuple[str, date]] = set()

        start_ts = datetime.combine(start, _MARKET_OPEN, tzinfo=_NY).astimezone(timezone.utc)
        equity_curve.append(
            {
                "date": str(start),
                "ts": start_ts.isoformat(),
                "equity": round(initial_capital, 2),
                "cash": round(initial_capital, 2),
                "positions": {},
            }
        )

        def _publish_progress(detail: str) -> None:
            if not self.progress_callback:
                return
            self.progress_callback(processed_decisions, total_decisions, detail)

        def _append_equity_point(ts: datetime) -> None:
            nonlocal peak_equity, max_drawdown
            equity_now = self._portfolio_equity_at(session, portfolio, ts)
            equity_curve.append(
                {
                    "date": str(ensure_utc(ts).astimezone(_NY).date()),
                    "ts": ensure_utc(ts).isoformat(),
                    "equity": round(equity_now, 2),
                    "cash": round(portfolio.cash, 2),
                    "positions": {
                        t: {"shares": p.shares, "side": p.side, "entry": p.avg_entry}
                        for t, p in portfolio.positions.items()
                    },
                }
            )
            peak_equity = max(peak_equity, equity_now)
            if peak_equity > 0:
                dd = (peak_equity - equity_now) / peak_equity
                max_drawdown = max(max_drawdown, dd)

        def _schedule_entry(entry: BTScheduledEntry) -> None:
            nonlocal schedule_order
            heappush(pending_entries, (entry.exec_ts, schedule_order, entry))
            schedule_order += 1

        def _schedule_exit(exit_plan: BTScheduledExit) -> None:
            nonlocal schedule_order
            heappush(pending_exits, (exit_plan.exec_ts, schedule_order, exit_plan))
            schedule_order += 1

        def _next_ready_ts() -> datetime | None:
            entry_ts = pending_entries[0][0] if pending_entries else None
            exit_ts = pending_exits[0][0] if pending_exits else None
            if entry_ts is None:
                return exit_ts
            if exit_ts is None:
                return entry_ts
            return exit_ts if exit_ts <= entry_ts else entry_ts

        def _flush_until(target_ts: datetime) -> None:
            while True:
                next_ts = _next_ready_ts()
                if next_ts is None or next_ts > target_ts:
                    break
                entry_ts = pending_entries[0][0] if pending_entries else None
                exit_ts = pending_exits[0][0] if pending_exits else None
                if exit_ts is not None and (entry_ts is None or exit_ts <= entry_ts):
                    _, _, exit_plan = heappop(pending_exits)
                    pos = portfolio.positions.get(exit_plan.ticker)
                    if not pos:
                        continue
                    pnl = (
                        (exit_plan.price - pos.avg_entry) * pos.shares
                        if pos.side == "LONG"
                        else (pos.avg_entry - exit_plan.price) * pos.shares
                    )
                    self._close_position(
                        portfolio,
                        exit_plan.ticker,
                        exit_plan.price,
                        ensure_utc(exit_plan.exec_ts).astimezone(_NY).date(),
                        all_trades,
                        exit_plan.reason,
                        exec_ts=exit_plan.exec_ts,
                    )
                    self._update_loss_streak(ticker_loss_streak, exit_plan.ticker, pnl)
                    _append_equity_point(exit_plan.exec_ts)
                    continue

                _, _, entry_plan = heappop(pending_entries)
                if entry_plan.ticker in portfolio.positions:
                    continue
                equity_now = self._portfolio_equity_at(session, portfolio, entry_plan.exec_ts)
                target_pct = min(entry_plan.pos_pct, max_pos_pct)
                trades_before = len(all_trades)
                self._execute_decision(
                    portfolio,
                    entry_plan.ticker,
                    entry_plan.action,
                    target_pct,
                    equity_now,
                    entry_plan.exec_price,
                    ensure_utc(entry_plan.exec_ts).astimezone(_NY).date(),
                    all_trades,
                    stop_loss_pct=entry_plan.stop_loss_pct,
                    exec_ts=entry_plan.exec_ts,
                )
                if len(all_trades) == trades_before:
                    continue

                if intraday_flatten:
                    close_bar = self._regular_close_bar_for_session_day(session, entry_plan.ticker, entry_plan.exec_ts)
                    if close_bar is not None and ensure_utc(close_bar.ts) > ensure_utc(entry_plan.exec_ts):
                        exit_px = float(close_bar.close)
                        pos = portfolio.positions.get(entry_plan.ticker)
                        if pos is not None and pos.side == "LONG":
                            exit_px *= 1 - slippage_pct
                        elif pos is not None and pos.side == "SHORT":
                            exit_px *= 1 + slippage_pct
                        _schedule_exit(
                            BTScheduledExit(
                                ticker=entry_plan.ticker,
                                exec_ts=ensure_utc(close_bar.ts),
                                price=exit_px,
                                reason="intraday_flatten",
                            )
                        )

        for idx, candidate in enumerate(candidates, start=1):
            event_ts = ensure_utc(candidate.as_of)
            _flush_until(event_ts)

            detail_prefix = (
                f"Processed {processed_decisions}/{total_decisions} agent triggers "
                f"({idx}/{len(candidates)} · {candidate.ticker} @ {event_ts.astimezone(_NY):%Y-%m-%d %H:%M})"
            )

            entry_bar = self._bar_at_or_after(
                session,
                candidate.ticker,
                event_ts + timedelta(minutes=1),
                regular_session_only=True,
            )
            if entry_bar is None:
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · no entry bar")
                continue

            entry_ts = ensure_utc(entry_bar.ts)
            entry_session_day = entry_ts.astimezone(_NY).date()
            if entry_session_day < start or entry_session_day > end:
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · entry outside backtest window")
                continue

            session_key = (candidate.ticker, entry_session_day)
            if session_key in used_session_keys:
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · duplicate ticker/session")
                continue
            if candidate.ticker in portfolio.positions:
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · existing open position")
                continue

            ticker_sector = _TICKER_SECTOR.get(candidate.ticker, "other")
            sector_positions = [
                t for t, p in portfolio.positions.items()
                if t != candidate.ticker and _TICKER_SECTOR.get(t, "other") == ticker_sector
            ]
            if len(sector_positions) >= _MAX_SECTOR_POSITIONS:
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · sector limit")
                continue

            equity_now = self._portfolio_equity_at(session, portfolio, event_ts)
            current_price = self._latest_price_at_or_before(session, candidate.ticker, event_ts, regular_session_only=True) or float(entry_bar.open)
            pos_value = portfolio.position_value(candidate.ticker, current_price)
            current_side = portfolio.positions[candidate.ticker].side if candidate.ticker in portfolio.positions else None

            local_session = make_session()
            try:
                bt_context = {
                    "portfolio_state": {
                        "position_pct": pos_value / max(equity_now, 1.0),
                        "daily_pnl": self._compute_daily_realized_pnl(
                            all_trades,
                            target_day=event_ts.astimezone(_NY).date(),
                            ticker=candidate.ticker,
                        ),
                        "equity": equity_now,
                        "current_side": current_side,
                        "drawdown_pct": max_drawdown,
                    },
                    "current_position": {
                        "side": current_side,
                        "shares": portfolio.positions[candidate.ticker].shares if candidate.ticker in portfolio.positions else 0,
                        "entry_price": portfolio.positions[candidate.ticker].avg_entry if candidate.ticker in portfolio.positions else 0,
                        "entry_date": str(portfolio.positions[candidate.ticker].entry_date) if candidate.ticker in portfolio.positions else None,
                    },
                    "portfolio_positions": {
                        t: {"side": p.side, "shares": p.shares, "entry": p.avg_entry}
                        for t, p in portfolio.positions.items()
                    },
                    "ticker_loss_streak": ticker_loss_streak,
                    "trigger_event_id": candidate.trigger_event["id"],
                    "trigger_event": candidate.trigger_event,
                    "allowed_sources": allowed_sources,
                }
                state = graph.run(local_session, candidate.ticker, context=bt_context, as_of=event_ts)
            except Exception as exc:
                errors.append(f"{candidate.ticker} {event_ts.isoformat()}: {exc}")
                processed_decisions += 1
                _publish_progress(f"{detail_prefix} · graph error")
                continue
            finally:
                local_session.close()

            action = state.get("final_action", "HOLD")
            pos_pct = float(state.get("final_position_pct", 0.0) or 0.0)
            reasoning = str(state.get("final_reasoning", "") or "")[:300]
            signals = {
                k: v.get("signal", "?") if isinstance(v, dict) else "?"
                for k, v in state.get("agent_signals", {}).items()
            }
            if action == "NO_SIGNAL":
                risk_sig = state.get("agent_signals", {}).get("risk_manager", {})
                if isinstance(risk_sig, dict) and risk_sig.get("metadata", {}).get("approved"):
                    rm_dir = risk_sig.get("signal", "HOLD")
                    rm_pct = risk_sig.get("metadata", {}).get("max_position_pct", 0.07)
                    if rm_dir in ("BUY", "SHORT"):
                        action = rm_dir
                        pos_pct = rm_pct
                        reasoning = f"[fallback from risk_manager] {risk_sig.get('reasoning', '')}"
                    else:
                        action = "HOLD"
                        pos_pct = 0.0
                else:
                    action = "HOLD"
                    pos_pct = 0.0

            decision = BTDecision(
                date=event_ts.astimezone(_NY).date(),
                ticker=candidate.ticker,
                action=action,
                position_pct=pos_pct,
                reasoning=reasoning,
                agent_signals=signals,
                ts=event_ts,
            )
            all_decisions.append(decision)

            if action in {"BUY", "SHORT"} and pos_pct > 0:
                entry_px = float(entry_bar.open)
                if action == "BUY":
                    entry_px *= 1 + slippage_pct
                else:
                    entry_px *= 1 - slippage_pct
                atr_stop = self._compute_atr_stop(
                    session,
                    candidate.ticker,
                    entry_session_day,
                    default_stop=stop_loss_pct,
                )
                _schedule_entry(
                    BTScheduledEntry(
                        ticker=candidate.ticker,
                        action=action,
                        pos_pct=pos_pct,
                        exec_ts=entry_ts,
                        exec_price=entry_px,
                        decision_ts=event_ts,
                        trigger_event_id=candidate.trigger_event["id"],
                        decision=decision,
                        stop_loss_pct=atr_stop,
                    )
                )
                used_session_keys.add(session_key)

            processed_decisions += 1
            _publish_progress(detail_prefix)

        final_flush_ts = datetime.combine(end + timedelta(days=1), _MARKET_CLOSE, tzinfo=_NY).astimezone(timezone.utc)
        _flush_until(final_flush_ts)

        final_day = trading_days[-1]
        final_close_ts = datetime.combine(final_day, _MARKET_CLOSE, tzinfo=_NY).astimezone(timezone.utc)
        for ticker in list(portfolio.positions.keys()):
            close_bar = self._regular_close_bar_for_session_day(session, ticker, final_close_ts)
            if close_bar is None:
                continue
            exit_px = float(close_bar.close)
            pos = portfolio.positions.get(ticker)
            if pos is not None and pos.side == "LONG":
                exit_px *= 1 - slippage_pct
            elif pos is not None and pos.side == "SHORT":
                exit_px *= 1 + slippage_pct
            pnl = (
                (exit_px - pos.avg_entry) * pos.shares
                if pos and pos.side == "LONG"
                else (pos.avg_entry - exit_px) * pos.shares if pos else 0.0
            )
            self._close_position(
                portfolio,
                ticker,
                exit_px,
                final_day,
                all_trades,
                "backtest_end",
                exec_ts=ensure_utc(close_bar.ts),
            )
            self._update_loss_streak(ticker_loss_streak, ticker, pnl)
            _append_equity_point(ensure_utc(close_bar.ts))

        final_equity = portfolio.cash
        total_return = (final_equity / initial_capital - 1) * 100
        trade_pnls = self._compute_trade_pnls(all_trades)
        winning = sum(1 for pnl in trade_pnls if pnl > 0)
        losing = sum(1 for pnl in trade_pnls if pnl < 0)
        stop_loss_count = sum(1 for t in all_trades if t.reason == "stop_loss")

        print(f"\n{'='*60}")
        print(f"[{_ts()}] ✅ BACKTEST COMPLETE")
        print(f"  Return: {total_return:+.2f}%  (${initial_capital:,.0f} → ${final_equity:,.0f})")
        print(f"  Max Drawdown: {max_drawdown * 100:.2f}%")
        print(f"  Trades: {len(all_trades)} ({winning}W / {losing}L)")
        print(f"  Trigger events processed: {len(candidates)}")
        print(f"{'='*60}\n")
        sys.stdout.flush()

        return AgentBacktestResult(
            start_date=start,
            end_date=end,
            tickers=tickers,
            initial_capital=initial_capital,
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return, 2),
            max_drawdown_pct=round(max_drawdown * 100, 2),
            total_trades=len(all_trades),
            winning_trades=winning,
            losing_trades=losing,
            stop_losses=stop_loss_count,
            equity_curve=equity_curve,
            trades=all_trades,
            decisions=all_decisions,
            errors=errors,
        )

    def _load_event_time_candidates(
        self,
        session: Session,
        *,
        tickers: list[str],
        start: date,
        end: date,
        allowed_sources: list[str] | None,
        use_event_quality_filter: bool,
        event_quality_min_score: int,
        event_quality_fail_open: bool,
    ) -> list[BTCandidateEvent]:
        start_ts = datetime.combine(start, dt_time(0, 0), tzinfo=_NY).astimezone(timezone.utc)
        end_ts = datetime.combine(end + timedelta(days=1), dt_time(0, 0), tzinfo=_NY).astimezone(timezone.utc)
        ticker_filters = [Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%') for ticker in tickers]
        conditions = [
            Event.confidence >= max(0, int(getattr(self.settings, "live_min_confidence", 50) or 0)),
            Event.event_time >= start_ts,
            Event.event_time < end_ts,
            sa.or_(*ticker_filters) if ticker_filters else sa.true(),
        ]
        if not use_event_quality_filter:
            conditions.append(Event.validation_status == "VALID")

        events = (
            session.execute(
                select(Event)
                .where(*conditions)
                .order_by(Event.event_time.asc(), Event.created_at.asc(), Event.id.asc())
            )
            .scalars()
            .all()
        )
        selected_tickers = {ticker.upper() for ticker in tickers}
        candidates: list[BTCandidateEvent] = []
        for event in events:
            ticker = self._primary_ticker_for_event(event, selected_tickers)
            if not ticker:
                continue
            evidences = session.execute(
                select(EventEvidence)
                .where(EventEvidence.event_id == event.id)
                .order_by(EventEvidence.source_tier.asc(), EventEvidence.captured_at.asc(), EventEvidence.id.asc())
            ).scalars().all()
            payload = self._event_payload(event, evidences, allowed_sources=allowed_sources)
            if payload is None:
                continue
            if use_event_quality_filter:
                quality = self.analysis.assess_event_quality(event, session=session)
                if quality.get("error") and not event_quality_fail_open:
                    continue
                if not quality.get("error"):
                    quality_score = int(quality.get("quality_score", 0) or 0)
                    if quality_score < event_quality_min_score:
                        continue
                payload = {
                    **payload,
                    "quality": str(quality.get("quality") or "UNKNOWN"),
                    "quality_score": int(quality.get("quality_score", 0) or 0),
                    "quality_reason": str(quality.get("reason") or ""),
                }
            else:
                tradeability = self.analysis.assess_tradeability(event, session=session)
                if not self._is_strong_tradeable_event(tradeability):
                    continue
            candidates.append(
                BTCandidateEvent(
                    ticker=ticker,
                    as_of=ensure_utc(event.event_time),
                    event=event,
                    trigger_event=payload,
                )
            )
        return candidates

    @staticmethod
    def _primary_ticker_for_event(event: Event, selected_tickers: set[str]) -> str | None:
        for ticker in event.tickers or []:
            normalized = str(ticker or "").strip().upper()
            if normalized and normalized in selected_tickers:
                return normalized
        return None

    def _find_trigger_event(
        self,
        session: Session,
        *,
        ticker: str,
        as_of: datetime,
        allowed_sources: list[str] | None,
        use_event_quality_filter: bool,
        event_quality_min_score: int,
        event_quality_fail_open: bool,
    ) -> dict[str, Any] | None:
        since = as_of - timedelta(hours=48)
        conditions = [
            Event.confidence >= max(0, int(getattr(self.settings, "live_min_confidence", 50) or 0)),
            Event.event_time >= since,
            Event.event_time <= as_of,
            Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%'),
        ]
        if not use_event_quality_filter:
            conditions.append(Event.validation_status == "VALID")
        stmt = (
            select(Event)
            .where(*conditions)
            .order_by(Event.event_time.desc(), Event.created_at.desc(), Event.id.desc())
            .limit(40 if use_event_quality_filter else 20)
        )
        events = session.execute(stmt).scalars().all()
        for event in events:
            evidences = session.execute(
                select(EventEvidence)
                .where(EventEvidence.event_id == event.id)
                .order_by(EventEvidence.source_tier.asc(), EventEvidence.captured_at.asc(), EventEvidence.id.asc())
            ).scalars().all()
            payload = self._event_payload(event, evidences, allowed_sources=allowed_sources)
            if payload is None:
                continue
            if use_event_quality_filter:
                quality = self.analysis.assess_event_quality(event, session=session)
                if quality.get("error") and not event_quality_fail_open:
                    continue
                if not quality.get("error"):
                    quality_score = int(quality.get("quality_score", 0) or 0)
                    if quality_score < event_quality_min_score:
                        continue
                payload = {
                    **payload,
                    "quality": str(quality.get("quality") or "UNKNOWN"),
                    "quality_score": int(quality.get("quality_score", 0) or 0),
                    "quality_reason": str(quality.get("reason") or ""),
                }
            else:
                tradeability = self.analysis.assess_tradeability(event, session=session)
                if not self._is_strong_tradeable_event(tradeability):
                    continue
            return payload
        return None

    @staticmethod
    def _event_payload(
        event: Event,
        evidences: list[EventEvidence],
        *,
        allowed_sources: list[str] | None,
    ) -> dict[str, Any] | None:
        if not evidences:
            return None
        allowed_source_set = {
            normalize_source_name(str(source).strip().lower())
            for source in (allowed_sources or [])
            if str(source).strip()
        }
        normalized_sources = {
            normalize_source_name(ev.source)
            for ev in evidences
            if getattr(ev, "source", None)
        }
        if allowed_source_set and not normalized_sources.intersection(allowed_source_set):
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

    @staticmethod
    def _is_strong_tradeable_event(tradeability: dict[str, Any]) -> bool:
        if not tradeability.get("tradeable", False):
            return False
        if int(tradeability.get("strong_sources", 0) or 0) < 1:
            return False
        if int(tradeability.get("hard_event_hits", 0) or 0) >= 1:
            return True
        if int(tradeability.get("ticker_specific_hits", 0) or 0) >= 1:
            return True
        return False

    def _bar_at_or_after(
        self,
        session: Session,
        ticker: str,
        ts: datetime,
        *,
        regular_session_only: bool = False,
    ) -> Bar1m | None:
        stmt = (
            select(Bar1m)
            .where(Bar1m.ticker == ticker.upper(), Bar1m.ts >= ensure_utc(ts))
            .order_by(Bar1m.ts.asc())
        )
        if not regular_session_only:
            return session.execute(stmt.limit(1)).scalars().first()

        search_end = ensure_utc(ts) + timedelta(days=5)
        bars = (
            session.execute(stmt.where(Bar1m.ts < search_end).limit(5000))
            .scalars()
            .all()
        )
        for bar in bars:
            if self._is_regular_session_bar(bar.ts):
                return bar
        return None

    def _latest_price_at_or_before(
        self,
        session: Session,
        ticker: str,
        ts: datetime,
        *,
        regular_session_only: bool = False,
    ) -> float | None:
        stmt = (
            select(Bar1m)
            .where(Bar1m.ticker == ticker.upper(), Bar1m.ts <= ensure_utc(ts))
            .order_by(Bar1m.ts.desc())
        )
        if not regular_session_only:
            bar = session.execute(stmt.limit(1)).scalars().first()
            return float(bar.close) if bar else None

        search_start = ensure_utc(ts) - timedelta(days=5)
        bars = (
            session.execute(stmt.where(Bar1m.ts >= search_start).limit(5000))
            .scalars()
            .all()
        )
        for bar in bars:
            if self._is_regular_session_bar(bar.ts):
                return float(bar.close)
        return None

    @staticmethod
    def _is_regular_session_bar(ts: datetime) -> bool:
        local = ensure_utc(ts).astimezone(_NY)
        if local.weekday() >= 5:
            return False
        local_clock = local.timetz().replace(tzinfo=None)
        return _MARKET_OPEN <= local_clock < _MARKET_CLOSE

    def _regular_close_bar_for_session_day(self, session: Session, ticker: str, ts: datetime) -> Bar1m | None:
        session_day = ensure_utc(ts).astimezone(_NY).date()
        ny_start = datetime.combine(session_day, _MARKET_OPEN, tzinfo=_NY)
        ny_end = datetime.combine(session_day, _MARKET_CLOSE, tzinfo=_NY)
        rth_start = ny_start.astimezone(timezone.utc)
        rth_end = ny_end.astimezone(timezone.utc)
        return (
            session.execute(
                select(Bar1m)
                .where(
                    Bar1m.ticker == ticker.upper(),
                    Bar1m.ts >= rth_start,
                    Bar1m.ts < rth_end,
                )
                .order_by(Bar1m.ts.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    def _portfolio_equity_at(self, session: Session, portfolio: BTPortfolio, ts: datetime) -> float:
        prices: dict[str, float] = {}
        for ticker, pos in portfolio.positions.items():
            price = self._latest_price_at_or_before(session, ticker, ts, regular_session_only=True)
            prices[ticker] = float(price or pos.avg_entry)
        return portfolio.equity(prices)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_trading_days(
        self, session: Session, start: date, end: date, ref_ticker: str = "SPY",
    ) -> list[date]:
        """Get trading days from bar data (weekdays where bars exist)."""
        rows = session.execute(
            select(func.date(Bar1m.ts))
            .where(
                Bar1m.ticker == ref_ticker.upper(),
                func.date(Bar1m.ts) >= start,
                func.date(Bar1m.ts) <= end,
            )
            .group_by(func.date(Bar1m.ts))
            .order_by(func.date(Bar1m.ts))
        ).all()
        # Filter out weekends — Finnhub after-hours bars can spill into Saturday UTC
        return [
            date.fromisoformat(str(r[0]))
            for r in rows if r[0] and date.fromisoformat(str(r[0])).weekday() < 5
        ]

    @staticmethod
    def _next_trading_day(trading_days: list[date], current: date) -> date | None:
        idx = trading_days.index(current) if current in trading_days else -1
        if idx < 0 or idx >= len(trading_days) - 1:
            return None
        return trading_days[idx + 1]

    def _get_price(
        self, session: Session, ticker: str, day: date, which: str = "open",
    ) -> float | None:
        """Get open or close price during regular trading hours for a ticker on a specific day.

        Convert the target trading day from New York session hours into UTC so
        DST transitions match the live trading path.
        """
        ny_start = datetime.combine(day, _MARKET_OPEN, tzinfo=_NY)
        ny_end = datetime.combine(day, _MARKET_CLOSE, tzinfo=_NY)
        rth_start = ny_start.astimezone(timezone.utc)
        rth_end = ny_end.astimezone(timezone.utc)

        if which == "open":
            bar = session.execute(
                select(Bar1m)
                .where(
                    Bar1m.ticker == ticker.upper(),
                    Bar1m.ts >= rth_start,
                    Bar1m.ts < rth_end,
                )
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).scalars().first()
            return float(bar.open) if bar else None
        else:
            bar = session.execute(
                select(Bar1m)
                .where(
                    Bar1m.ticker == ticker.upper(),
                    Bar1m.ts >= rth_start,
                    Bar1m.ts < rth_end,
                )
                .order_by(Bar1m.ts.desc())
                .limit(1)
            ).scalars().first()
            return float(bar.close) if bar else None

    def _get_close_prices(
        self, session: Session, tickers: list[str], day: date,
    ) -> dict[str, float]:
        prices = {}
        for ticker in tickers:
            price = self._get_price(session, ticker, day, "close")
            if price:
                prices[ticker] = price
        return prices

    def _compute_atr_stop(
        self,
        session: Session,
        ticker: str,
        as_of: date,
        default_stop: float = 0.05,
        atr_mult: float = 2.5,
        min_stop: float = 0.03,
        max_stop: float = 0.08,
        lookback_days: int = 90,
    ) -> float:
        """Compute ATR-based stop-loss percentage from recent daily ranges.

        Uses a 90-day lookback so it can find data across gaps in bar history.
        Groups Bar1m data into daily buckets and computes average high-low range
        as a fraction of the last close. Returns 2.5×ATR clamped to [min_stop,
        max_stop]. Falls back to default_stop when fewer than 3 days are found.
        """
        cutoff = datetime.combine(as_of, dt_time(0, 0), tzinfo=timezone.utc)
        lookback_start = cutoff - timedelta(days=lookback_days)
        day_expr = func.date(Bar1m.ts)

        rows = session.execute(
            select(
                day_expr.label("day"),
                func.max(Bar1m.high).label("day_high"),
                func.min(Bar1m.low).label("day_low"),
                func.avg(Bar1m.close).label("day_close"),
            )
            .where(
                Bar1m.ticker == ticker.upper(),
                Bar1m.ts >= lookback_start,
                Bar1m.ts < cutoff,
            )
            .group_by(day_expr)
            .order_by(day_expr.desc())
            .limit(20)
        ).all()

        if len(rows) < 3:
            return default_stop

        daily_ranges = [float(r.day_high) - float(r.day_low) for r in rows]
        avg_range = sum(daily_ranges) / len(daily_ranges)
        last_close = float(rows[0].day_close)
        if last_close <= 0:
            return default_stop

        atr_pct = (avg_range / last_close) * atr_mult
        return round(max(min_stop, min(max_stop, atr_pct)), 4)

    def _execute_decision(
        self,
        portfolio: BTPortfolio,
        ticker: str,
        action: str,
        target_pct: float,
        equity: float,
        price: float,
        exec_date: date,
        trades: list[BTTrade],
        stop_loss_pct: float = 0.03,
        exec_ts: datetime | None = None,
    ) -> None:
        """Execute a BUY or SHORT decision, adjusting position to target."""
        target_notional = equity * target_pct
        target_shares = target_notional / price if price > 0 else 0

        current_pos = portfolio.positions.get(ticker)

        if action == "BUY":
            if current_pos and current_pos.side == "SHORT":
                # Close short first
                self._close_position(portfolio, ticker, price, exec_date, trades, "reverse_to_long")
                current_pos = None

            current_shares = current_pos.shares if current_pos else 0
            shares_to_buy = target_shares - current_shares
            if shares_to_buy > 0:
                cost = shares_to_buy * price
                if cost > portfolio.cash:
                    shares_to_buy = portfolio.cash / price
                    cost = shares_to_buy * price
                if shares_to_buy > 0:
                    portfolio.cash -= cost
                    if current_pos:
                        total_cost = current_pos.shares * current_pos.avg_entry + cost
                        current_pos.shares += shares_to_buy
                        current_pos.avg_entry = total_cost / current_pos.shares
                    else:
                        portfolio.positions[ticker] = BTPosition(
                            ticker=ticker, shares=shares_to_buy,
                            avg_entry=price, side="LONG", entry_date=exec_date,
                            entry_ts=exec_ts,
                            stop_loss_pct=stop_loss_pct,
                        )
                    trades.append(BTTrade(
                        date=exec_date, ticker=ticker, side="BUY",
                        shares=round(shares_to_buy, 4), price=price,
                        notional=round(cost, 2), reason=f"agent_buy_{target_pct:.0%}",
                        ts=exec_ts,
                    ))

        elif action == "SHORT":
            if current_pos and current_pos.side == "LONG":
                self._close_position(portfolio, ticker, price, exec_date, trades, "reverse_to_short")
                current_pos = None

            current_shares = current_pos.shares if current_pos else 0
            shares_to_short = target_shares - current_shares
            if shares_to_short > 0:
                proceeds = shares_to_short * price
                portfolio.cash += proceeds
                if current_pos:
                    current_pos.shares += shares_to_short
                else:
                    portfolio.positions[ticker] = BTPosition(
                        ticker=ticker, shares=shares_to_short,
                        avg_entry=price, side="SHORT", entry_date=exec_date,
                        entry_ts=exec_ts,
                        stop_loss_pct=stop_loss_pct,
                    )
                trades.append(BTTrade(
                    date=exec_date, ticker=ticker, side="SHORT",
                    shares=round(shares_to_short, 4), price=price,
                    notional=round(proceeds, 2), reason=f"agent_short_{target_pct:.0%}",
                    ts=exec_ts,
                ))

    def _close_position(
        self,
        portfolio: BTPortfolio,
        ticker: str,
        price: float,
        close_date: date,
        trades: list[BTTrade],
        reason: str,
        exec_ts: datetime | None = None,
    ) -> None:
        pos = portfolio.positions.pop(ticker, None)
        if not pos:
            return

        if pos.side == "LONG":
            proceeds = pos.shares * price
            portfolio.cash += proceeds
            trades.append(BTTrade(
                date=close_date, ticker=ticker, side="SELL",
                shares=round(pos.shares, 4), price=price,
                notional=round(proceeds, 2), reason=reason,
                ts=exec_ts,
            ))
        else:
            # Short close: buy back shares at current price.
            # Original short sale proceeds are already in cash.
            cost = pos.shares * price
            portfolio.cash -= cost
            trades.append(BTTrade(
                date=close_date, ticker=ticker, side="COVER",
                shares=round(pos.shares, 4), price=price,
                notional=round(cost, 2), reason=reason,
                ts=exec_ts,
            ))

    @staticmethod
    def _compute_trade_pnls(trades: list[BTTrade]) -> list[float]:
        """Compute P&L for round-trip trades (BUY→SELL, SHORT→COVER)."""
        open_trades: dict[str, list[BTTrade]] = {}
        pnls: list[float] = []

        for t in trades:
            if t.side in ("BUY", "SHORT"):
                open_trades.setdefault(t.ticker, []).append(t)
            elif t.side == "SELL" and t.ticker in open_trades:
                opens = open_trades[t.ticker]
                if opens:
                    entry = opens.pop(0)
                    pnls.append((t.price - entry.price) * entry.shares)
            elif t.side == "COVER" and t.ticker in open_trades:
                opens = open_trades[t.ticker]
                if opens:
                    entry = opens.pop(0)
                    pnls.append((entry.price - t.price) * entry.shares)

        return pnls

    @staticmethod
    def _compute_daily_realized_pnl(
        trades: list[BTTrade],
        target_day: date,
        ticker: str | None = None,
    ) -> float:
        """Compute realized P&L booked on `target_day`.

        This is intentionally close-date based. Opening cash flows should not be
        treated as losses. PnL is realized only when an open trade is matched by
        a SELL/COVER leg, including closes of positions opened on earlier days.
        """
        open_trades: dict[str, list[BTTrade]] = {}
        realized = 0.0

        for trade in trades:
            if ticker and trade.ticker != ticker:
                continue
            if trade.side in ("BUY", "SHORT"):
                open_trades.setdefault(trade.ticker, []).append(trade)
                continue

            opens = open_trades.get(trade.ticker, [])
            if not opens:
                continue

            entry = opens.pop(0)
            pnl = 0.0
            if trade.side == "SELL":
                pnl = (trade.price - entry.price) * entry.shares
            elif trade.side == "COVER":
                pnl = (entry.price - trade.price) * entry.shares

            if trade.date == target_day:
                realized += pnl

        return realized

    @staticmethod
    def _update_loss_streak(
        ticker_loss_streak: dict[str, int], ticker: str, pnl: float,
    ) -> None:
        """Update consecutive loss counter for a ticker."""
        if pnl < 0:
            ticker_loss_streak[ticker] = ticker_loss_streak.get(ticker, 0) + 1
        else:
            ticker_loss_streak[ticker] = 0
