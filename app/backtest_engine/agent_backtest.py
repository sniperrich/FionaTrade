"""Agent-mode backtest engine with strict temporal isolation.

Runs the full agent graph (MacroAnalyst → News → Fundamentals → Technicals →
RiskManager → PortfolioManager) day-by-day over historical data, ensuring no
look-ahead bias: all data queries are bounded by the simulation timestamp.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.agent_graph.graph import AgentGraph
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.db.models import Bar1m

logger = get_app_logger()

_NY = ZoneInfo("America/New_York")
_MARKET_CLOSE = dt_time(16, 0)
_MARKET_OPEN = dt_time(9, 30)


def _ts() -> str:
    """Compact timestamp for progress output."""
    return datetime.now().strftime("%H:%M:%S")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BTPosition:
    ticker: str
    shares: float
    avg_entry: float
    side: str  # LONG or SHORT
    entry_date: date


@dataclass
class BTTrade:
    date: date
    ticker: str
    side: str  # BUY, SELL, SHORT, COVER
    shares: float
    price: float
    notional: float
    reason: str


@dataclass
class BTDecision:
    date: date
    ticker: str
    action: str
    position_pct: float
    reasoning: str
    agent_signals: dict


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
                eq -= pos.shares * (price - pos.avg_entry)
                eq += pos.shares * pos.avg_entry
        return eq

    def position_value(self, ticker: str, price: float) -> float:
        pos = self.positions.get(ticker)
        if not pos:
            return 0.0
        return pos.shares * price


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
    equity_curve: list[dict]
    trades: list[BTTrade]
    decisions: list[BTDecision]
    errors: list[str]


# ── Engine ────────────────────────────────────────────────────────────────────

class AgentBacktestEngine:
    """Backtest the agent pipeline over historical data."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, session: Session, params: dict | None = None) -> AgentBacktestResult:
        """Execute an agent-mode backtest.

        Params:
            tickers: list of ticker symbols (default: ["AAPL", "NVDA", "JPM", "XOM", "AMZN"])
            start_date: "YYYY-MM-DD" (default: "2026-02-02")
            end_date: "YYYY-MM-DD" (default: "2026-02-27")
            initial_capital: float (default: 100000)
            decision_frequency: int trading days between decisions (default: 3)
            max_position_pct: float max per-ticker position (default: 0.15)
        """
        p = params or {}
        tickers = p.get("tickers", ["AAPL", "NVDA", "JPM", "XOM", "AMZN"])
        start = date.fromisoformat(p.get("start_date", "2026-02-02"))
        end = date.fromisoformat(p.get("end_date", "2026-02-27"))
        initial_capital = float(p.get("initial_capital", 100_000))
        freq = int(p.get("decision_frequency", 3))
        max_pos_pct = float(p.get("max_position_pct", 0.15))

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
        graph = AgentGraph(self.settings)

        peak_equity = initial_capital
        max_drawdown = 0.0

        for day_idx, day in enumerate(trading_days):
            is_decision_day = day in decision_days

            if is_decision_day:
                # Run agent graph for each ticker at market close
                as_of = datetime.combine(day, _MARKET_CLOSE, tzinfo=_NY).astimezone(timezone.utc)
                decision_idx = decision_days.index(day) + 1
                print(f"\n[{_ts()}] 📊 Decision Day {decision_idx}/{len(decision_days)}: {day}")
                sys.stdout.flush()

                for ticker in tickers:
                    try:
                        t0 = time.time()
                        print(f"  [{_ts()}] 🤖 Running agents for {ticker}...", end="", flush=True)
                        state = graph.run(session, ticker, as_of=as_of)
                        elapsed = time.time() - t0

                        action = state.get("final_action", "HOLD")
                        pos_pct = float(state.get("final_position_pct", 0.0))
                        reasoning = state.get("final_reasoning", "")[:300]

                        signals = {
                            k: v.get("signal", "?") if isinstance(v, dict) else "?"
                            for k, v in state.get("agent_signals", {}).items()
                        }
                        signal_str = " ".join(f"{k[:4]}={v}" for k, v in signals.items())
                        print(f" → {action} {pos_pct:.0%} ({elapsed:.0f}s) [{signal_str}]")
                        sys.stdout.flush()

                        all_decisions.append(BTDecision(
                            date=day, ticker=ticker, action=action,
                            position_pct=pos_pct, reasoning=reasoning,
                            agent_signals=signals,
                        ))

                        # Execute trade at next day's open
                        next_day = self._next_trading_day(trading_days, day)
                        if next_day and action in ("BUY", "SHORT"):
                            open_price = self._get_price(session, ticker, next_day, "open")
                            if open_price and open_price > 0:
                                target_pct = min(pos_pct, max_pos_pct)
                                equity = portfolio.equity(
                                    self._get_close_prices(session, tickers, day)
                                )
                                trades_before = len(all_trades)
                                self._execute_decision(
                                    portfolio, ticker, action, target_pct,
                                    equity, open_price, next_day, all_trades,
                                )
                                if len(all_trades) > trades_before:
                                    t = all_trades[-1]
                                    print(f"    💰 TRADE: {t.side} {t.shares:.2f} {t.ticker} @ ${t.price:.2f} (${t.notional:,.0f})")
                                    sys.stdout.flush()
                        elif action == "HOLD":
                            pass  # Keep existing position

                        # Rate limit between LLM calls
                        time.sleep(2)

                    except Exception as exc:
                        err = f"Day {day} {ticker}: {exc}"
                        logger.warning("[agent_backtest] %s", err)
                        errors.append(err)

            # Mark-to-market at close
            close_prices = self._get_close_prices(session, tickers, day)
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
                    "[agent_backtest] %s: Equity=$%,.2f  Cash=$%,.2f  Positions=%d",
                    day, equity, portfolio.cash, len(portfolio.positions),
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

        print(f"\n{'='*60}")
        print(f"[{_ts()}] ✅ BACKTEST COMPLETE")
        print(f"  Return: {total_return:+.2f}%  (${initial_capital:,.0f} → ${final_equity:,.0f})")
        print(f"  Max Drawdown: {max_drawdown * 100:.2f}%")
        print(f"  Trades: {len(all_trades)} ({winning}W / {losing}L)")
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
            equity_curve=equity_curve, trades=all_trades,
            decisions=all_decisions, errors=errors,
        )

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
        """Get open or close price for a ticker on a specific day."""
        if which == "open":
            # First bar of the day
            bar = session.execute(
                select(Bar1m)
                .where(
                    Bar1m.ticker == ticker.upper(),
                    func.date(Bar1m.ts) == day,
                )
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).scalars().first()
            return float(bar.open) if bar else None
        else:
            # Last bar of the day
            bar = session.execute(
                select(Bar1m)
                .where(
                    Bar1m.ticker == ticker.upper(),
                    func.date(Bar1m.ts) == day,
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
                        )
                    trades.append(BTTrade(
                        date=exec_date, ticker=ticker, side="BUY",
                        shares=round(shares_to_buy, 4), price=price,
                        notional=round(cost, 2), reason=f"agent_buy_{target_pct:.0%}",
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
                    )
                trades.append(BTTrade(
                    date=exec_date, ticker=ticker, side="SHORT",
                    shares=round(shares_to_short, 4), price=price,
                    notional=round(proceeds, 2), reason=f"agent_short_{target_pct:.0%}",
                ))

    def _close_position(
        self,
        portfolio: BTPortfolio,
        ticker: str,
        price: float,
        close_date: date,
        trades: list[BTTrade],
        reason: str,
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
            ))
        else:
            cost = pos.shares * price
            portfolio.cash -= cost
            # P&L = (entry - exit) * shares for short
            pnl = (pos.avg_entry - price) * pos.shares
            portfolio.cash += pos.shares * pos.avg_entry  # return collateral
            trades.append(BTTrade(
                date=close_date, ticker=ticker, side="COVER",
                shares=round(pos.shares, 4), price=price,
                notional=round(cost, 2), reason=reason,
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
