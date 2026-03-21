from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import ta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.market_data import MarketDataService

_NY = ZoneInfo("America/New_York")
_MARKET_DATA_SERVICE = MarketDataService(get_settings())


def get_bars(
    session: Session,
    ticker: str,
    lookback_bars: int = 390,
    end_time: datetime | None = None,
    regular_hours_only: bool = True,
) -> pd.DataFrame:
    """Load 1-minute bars from DB into a pandas DataFrame.

    Args:
        regular_hours_only: If True, filter to regular trading hours
            (9:30-16:00 ET) to exclude pre/after-market noise.
    Returns DataFrame with columns: ts, open, high, low, close, volume.
    Sorted ascending by ts.
    """
    end = end_time or datetime.now(timezone.utc)
    # Use calendar days (not minutes) to ensure we bridge weekends/holidays
    # Fetch extra bars to compensate for RTH filtering (~62% of bars are RTH)
    fetch_bars = int(lookback_bars * 1.8) if regular_hours_only else lookback_bars
    rows = _MARKET_DATA_SERVICE.load_analysis_rows(
        session,
        ticker=ticker,
        lookback_bars=fetch_bars,
        end_time=end,
        regular_hours_only=regular_hours_only,
    )

    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    data = [
        {
            "ts": r.ts,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.tail(lookback_bars).reset_index(drop=True)
    return df


def _is_regular_hours(ts: datetime) -> bool:
    """Check if a bar timestamp falls within regular US market hours (9:30-16:00 ET)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    et = ts.astimezone(_NY)
    t = et.time()
    from datetime import time as dt_time
    return dt_time(9, 30) <= t < dt_time(16, 0)


def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute common technical indicators from a bar DataFrame.

    Returns a dict with scalar indicator values and their signals.
    Requires at least 26 rows for MACD; fewer rows degrade gracefully.
    """
    if len(df) < 5:
        return {"error": "insufficient_data", "bar_count": len(df)}

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    result: dict = {"bar_count": len(df)}

    # ── RSI ──────────────────────────────────────────────────────────────────
    if len(df) >= 15:
        rsi_indicator = ta.momentum.RSIIndicator(close, window=14)
        rsi_series = rsi_indicator.rsi()
        rsi_val = float(rsi_series.iloc[-1]) if not rsi_series.empty else None
        result["rsi"] = round(rsi_val, 1) if rsi_val is not None else None
        if rsi_val is not None:
            if rsi_val >= 70:
                result["rsi_signal"] = "overbought"
            elif rsi_val <= 30:
                result["rsi_signal"] = "oversold"
            else:
                result["rsi_signal"] = "neutral"

    # ── MACD ─────────────────────────────────────────────────────────────────
    if len(df) >= 26:
        macd_indicator = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
        macd_line = macd_indicator.macd()
        macd_signal = macd_indicator.macd_signal()
        macd_hist = macd_indicator.macd_diff()

        if not macd_hist.empty:
            result["macd"] = round(float(macd_line.iloc[-1]), 4)
            result["macd_signal_line"] = round(float(macd_signal.iloc[-1]), 4)
            result["macd_histogram"] = round(float(macd_hist.iloc[-1]), 4)

            prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else 0.0
            curr_hist = float(macd_hist.iloc[-1])
            if curr_hist > 0 and prev_hist <= 0:
                result["macd_signal"] = "bullish_crossover"
            elif curr_hist < 0 and prev_hist >= 0:
                result["macd_signal"] = "bearish_crossover"
            elif curr_hist > 0:
                result["macd_signal"] = "bullish"
            else:
                result["macd_signal"] = "bearish"

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    if len(df) >= 20:
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper = bb.bollinger_hband()
        bb_lower = bb.bollinger_lband()
        bb_mid = bb.bollinger_mavg()
        bb_pct = bb.bollinger_pband()

        if not bb_upper.empty:
            current_close = float(close.iloc[-1])
            result["bb_upper"] = round(float(bb_upper.iloc[-1]), 2)
            result["bb_mid"] = round(float(bb_mid.iloc[-1]), 2)
            result["bb_lower"] = round(float(bb_lower.iloc[-1]), 2)
            result["bb_pct"] = round(float(bb_pct.iloc[-1]), 3)

            if current_close > float(bb_upper.iloc[-1]):
                result["bb_signal"] = "above_upper"
            elif current_close < float(bb_lower.iloc[-1]):
                result["bb_signal"] = "below_lower"
            else:
                result["bb_signal"] = "within_bands"

    # ── EMA trend ─────────────────────────────────────────────────────────────
    if len(df) >= 20:
        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator()
        current_close = float(close.iloc[-1])
        ema20_val = float(ema20.iloc[-1])
        result["ema20"] = round(ema20_val, 2)
        result["price_vs_ema20_pct"] = round((current_close / ema20_val - 1) * 100, 2)

    if len(df) >= 50:
        ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        ema50_val = float(ema50.iloc[-1])
        result["ema50"] = round(ema50_val, 2)
        ema20_val = result.get("ema20", float(close.iloc[-1]))
        if ema20_val > ema50_val:
            result["ema_trend"] = "bullish"
        else:
            result["ema_trend"] = "bearish"

    # ── Volume ───────────────────────────────────────────────────────────────
    if len(df) >= 20:
        avg_volume = float(volume.tail(20).mean())
        current_volume = float(volume.iloc[-1])
        result["volume_ratio"] = round(current_volume / avg_volume, 2) if avg_volume > 0 else None

    # ── Current price ─────────────────────────────────────────────────────────
    result["current_price"] = round(float(close.iloc[-1]), 2)
    result["price_change_pct"] = round(
        (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2
    ) if len(df) > 1 else 0.0

    return result


def get_technical_summary(
    session: Session, ticker: str, lookback_bars: int = 390, as_of: datetime | None = None,
) -> dict:
    """Convenience: load bars and compute indicators in one call."""
    df = get_bars(session, ticker, lookback_bars=lookback_bars, end_time=as_of)
    if df.empty:
        return {"ticker": ticker, "error": "no_data"}
    indicators = compute_indicators(df)
    indicators["ticker"] = ticker
    return indicators


def get_multi_day_performance(
    session: Session, ticker: str, days: int = 5, as_of: datetime | None = None,
) -> dict:
    """Compute multi-day price performance from daily OHLCV aggregated from 1m bars.
    
    Returns dict with daily_returns, period_return, avg_volume, etc.
    """
    # Fetch enough bars to cover N trading days (~390 bars/day × days)
    bars_needed = 390 * (days + 1)
    df = get_bars(session, ticker, lookback_bars=bars_needed, end_time=as_of)
    if df.empty or len(df) < 20:
        return {"ticker": ticker, "error": "insufficient_data"}

    df["date"] = pd.to_datetime(df["ts"]).dt.date
    daily = df.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).sort_index()

    if len(daily) < 2:
        return {"ticker": ticker, "error": "insufficient_daily_data"}

    daily["return_pct"] = daily["close"].pct_change() * 100
    recent_days = daily.tail(days + 1)

    daily_returns = []
    for date_val, row in recent_days.iterrows():
        if pd.notna(row["return_pct"]):
            daily_returns.append({
                "date": str(date_val),
                "close": round(float(row["close"]), 2),
                "change_pct": round(float(row["return_pct"]), 2),
                "volume": int(row["volume"]),
            })

    period_return = round(
        (float(recent_days["close"].iloc[-1]) / float(recent_days["close"].iloc[0]) - 1) * 100, 2
    ) if len(recent_days) >= 2 else 0.0

    return {
        "ticker": ticker,
        "days": len(daily_returns),
        "period_return_pct": period_return,
        "current_price": round(float(daily["close"].iloc[-1]), 2),
        "avg_daily_volume": int(daily["volume"].tail(days).mean()),
        "daily_returns": daily_returns,
        "high_of_period": round(float(recent_days["high"].max()), 2),
        "low_of_period": round(float(recent_days["low"].min()), 2),
    }


def get_spy_market_context(session: Session, as_of: datetime | None = None) -> dict:
    """Get SPY (S&P 500 ETF) recent performance as broad market context."""
    return get_multi_day_performance(session, "SPY", days=5, as_of=as_of)


def build_market_context_text(
    session: Session, ticker: str, as_of: datetime | None = None,
) -> str:
    """Build enriched market context for agents: SPY index + ticker multi-day performance."""
    lines: list[str] = []

    # SPY / broad market
    spy = get_spy_market_context(session, as_of=as_of)
    if "error" not in spy:
        lines.append("=== BROAD MARKET (SPY) ===")
        lines.append(f"  Current: ${spy['current_price']}  |  {spy['days']}-day return: {spy['period_return_pct']:+.2f}%")
        lines.append(f"  Range: ${spy['low_of_period']} – ${spy['high_of_period']}")
        for d in spy.get("daily_returns", [])[-5:]:
            lines.append(f"    {d['date']}: ${d['close']} ({d['change_pct']:+.2f}%)")
    else:
        lines.append("=== BROAD MARKET (SPY) ===\n  No SPY data available.")

    # Target ticker multi-day
    if ticker.upper() != "SPY":
        perf = get_multi_day_performance(session, ticker, days=5, as_of=as_of)
        if "error" not in perf:
            lines.append(f"\n=== {ticker} MULTI-DAY PERFORMANCE ===")
            lines.append(f"  Current: ${perf['current_price']}  |  {perf['days']}-day return: {perf['period_return_pct']:+.2f}%")
            lines.append(f"  Range: ${perf['low_of_period']} – ${perf['high_of_period']}  |  Avg Vol: {perf['avg_daily_volume']:,}")
            for d in perf.get("daily_returns", [])[-5:]:
                lines.append(f"    {d['date']}: ${d['close']} ({d['change_pct']:+.2f}%)")
        else:
            lines.append(f"\n=== {ticker} MULTI-DAY PERFORMANCE ===\n  No multi-day data available.")

    return "\n".join(lines)
