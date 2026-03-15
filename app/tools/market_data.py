from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import ta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Bar1m


def get_bars(
    session: Session,
    ticker: str,
    lookback_bars: int = 390,
    end_time: datetime | None = None,
) -> pd.DataFrame:
    """Load 1-minute bars from DB into a pandas DataFrame.

    Returns DataFrame with columns: ts, open, high, low, close, volume.
    Sorted ascending by ts.
    """
    end = end_time or datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_bars * 2)  # fetch extra to ensure we have enough

    rows = session.execute(
        select(Bar1m)
        .where(
            Bar1m.ticker == ticker.upper(),
            Bar1m.ts >= start,
            Bar1m.ts <= end,
        )
        .order_by(Bar1m.ts.asc())
        .limit(lookback_bars + 100)
    ).scalars().all()

    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(
        [
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
    )
    df = df.tail(lookback_bars).reset_index(drop=True)
    return df


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


def get_technical_summary(session: Session, ticker: str, lookback_bars: int = 390) -> dict:
    """Convenience: load bars and compute indicators in one call."""
    df = get_bars(session, ticker, lookback_bars=lookback_bars)
    if df.empty:
        return {"ticker": ticker, "error": "no_data"}
    indicators = compute_indicators(df)
    indicators["ticker"] = ticker
    return indicators


def build_technicals_context_text(session: Session, ticker: str, lookback_bars: int = 390) -> str:
    """Build a compact text block of technical indicators for LLM prompts."""
    data = get_technical_summary(session, ticker, lookback_bars)

    if "error" in data:
        return f"=== TECHNICALS FOR {ticker} ===\n  Error: {data['error']}"

    lines = [f"=== TECHNICALS FOR {ticker} ({data.get('bar_count', '?')} bars) ==="]
    lines.append(f"  Current Price: ${data.get('current_price', 'N/A')}")
    lines.append(f"  Session Change: {data.get('price_change_pct', 0):+.2f}%")

    if "rsi" in data:
        lines.append(f"  RSI(14): {data['rsi']} [{data.get('rsi_signal', '')}]")
    if "macd_histogram" in data:
        lines.append(
            f"  MACD Histogram: {data['macd_histogram']:+.4f} [{data.get('macd_signal', '')}]"
        )
    if "bb_pct" in data:
        lines.append(f"  BB %B: {data['bb_pct']:.3f} [{data.get('bb_signal', '')}]")
    if "ema_trend" in data:
        lines.append(
            f"  EMA20/50 Trend: {data.get('ema_trend', 'N/A')} "
            f"(price vs EMA20: {data.get('price_vs_ema20_pct', 0):+.2f}%)"
        )
    if "volume_ratio" in data:
        lines.append(f"  Volume vs 20-bar avg: {data['volume_ratio']:.2f}x")

    return "\n".join(lines)
