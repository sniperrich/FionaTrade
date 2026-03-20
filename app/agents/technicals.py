from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.tools.market_data import get_technical_summary, get_multi_day_performance

logger = get_app_logger()

# Pure rules thresholds
_RSI_OVERBOUGHT = 70
_RSI_OVERSOLD = 30
_RSI_MILDLY_OVERBOUGHT = 60
_RSI_MILDLY_OVERSOLD = 40
_VOLUME_SURGE = 2.0


class TechnicalsAgent(BaseAgent):
    """Analyzes technical indicators using pure rules (no LLM).

    Uses RSI, MACD, Bollinger Bands, EMA trend, and volume to determine
    short-term directional bias.
    """

    name = "technicals"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            as_of = (context or {}).get("as_of")
            data = get_technical_summary(
                session, ticker, lookback_bars=self.settings.agent_technicals_lookback_bars,
                as_of=as_of,
            )

            if "error" in data:
                return AgentSignal.no_signal(self.name, f"No bar data: {data['error']}")

            score = 0  # range roughly -100 to +100
            signals_fired: list[str] = []

            # ── RSI ───────────────────────────────────────────────────────────
            rsi = data.get("rsi")
            if rsi is not None:
                if rsi <= _RSI_OVERSOLD:
                    score += 30
                    signals_fired.append(f"RSI oversold ({rsi})")
                elif rsi <= _RSI_MILDLY_OVERSOLD:
                    score += 15
                    signals_fired.append(f"RSI mildly oversold ({rsi})")
                elif rsi >= _RSI_OVERBOUGHT:
                    score -= 30
                    signals_fired.append(f"RSI overbought ({rsi})")
                elif rsi >= _RSI_MILDLY_OVERBOUGHT:
                    score -= 15
                    signals_fired.append(f"RSI mildly overbought ({rsi})")

            # ── MACD ─────────────────────────────────────────────────────────
            macd_sig = data.get("macd_signal")
            if macd_sig == "bullish_crossover":
                score += 35
                signals_fired.append("MACD bullish crossover")
            elif macd_sig == "bearish_crossover":
                score -= 35
                signals_fired.append("MACD bearish crossover")
            elif macd_sig == "bullish":
                score += 10
                signals_fired.append("MACD bullish")
            elif macd_sig == "bearish":
                score -= 10
                signals_fired.append("MACD bearish")

            # ── Bollinger Bands ───────────────────────────────────────────────
            bb_sig = data.get("bb_signal")
            if bb_sig == "below_lower":
                score += 20
                signals_fired.append("Price below BB lower (oversold)")
            elif bb_sig == "above_upper":
                score -= 20
                signals_fired.append("Price above BB upper (overbought)")

            # ── EMA Trend ─────────────────────────────────────────────────────
            ema_trend = data.get("ema_trend")
            if ema_trend == "bullish":
                score += 15
                signals_fired.append("EMA20 > EMA50 (uptrend)")
            elif ema_trend == "bearish":
                score -= 15
                signals_fired.append("EMA20 < EMA50 (downtrend)")

            # ── Volume confirmation ───────────────────────────────────────────
            vol_ratio = data.get("volume_ratio")
            if vol_ratio is not None and vol_ratio >= _VOLUME_SURGE:
                # Amplify existing signal on high volume
                if score > 0:
                    score += 10
                    signals_fired.append(f"High volume confirmation ({vol_ratio:.1f}x avg)")
                elif score < 0:
                    score -= 10
                    signals_fired.append(f"High volume selling pressure ({vol_ratio:.1f}x avg)")

            # ── Relative strength vs SPY ─────────────────────────────────────
            if ticker.upper() != "SPY":
                spy_perf = get_multi_day_performance(session, "SPY", days=5, as_of=as_of)
                ticker_perf = get_multi_day_performance(session, ticker, days=5, as_of=as_of)
                if "error" not in spy_perf and "error" not in ticker_perf:
                    spy_ret = spy_perf.get("period_return_pct", 0)
                    tk_ret = ticker_perf.get("period_return_pct", 0)
                    relative = tk_ret - spy_ret
                    if relative > 3.0:
                        score += 10
                        signals_fired.append(f"Outperforming SPY by {relative:+.1f}pp (5d)")
                    elif relative < -3.0:
                        score -= 10
                        signals_fired.append(f"Underperforming SPY by {relative:+.1f}pp (5d)")

            # ── Map score to signal and confidence ────────────────────────────
            abs_score = abs(score)
            confidence = min(95, int(abs_score * 1.2))  # scale to 0-95

            if score >= 15:
                signal = "BUY"
            elif score <= -15:
                signal = "SHORT"
            else:
                signal = "HOLD"
                confidence = max(20, confidence)  # HOLD is moderately confident

            reasoning = (
                f"Technical score: {score:+d}. "
                + (f"Signals: {', '.join(signals_fired)}." if signals_fired else "No strong signals.")
            )

            return AgentSignal(
                agent_name=self.name,
                signal=signal,
                confidence=confidence,
                reasoning=reasoning,
                metadata={
                    "score": score,
                    "signals_fired": signals_fired,
                    "rsi": rsi,
                    "macd_signal": macd_sig,
                    "bb_signal": bb_sig,
                    "ema_trend": ema_trend,
                    "volume_ratio": vol_ratio,
                    "current_price": data.get("current_price"),
                    "price_change_pct": data.get("price_change_pct"),
                    "bar_count": data.get("bar_count"),
                },
            )

        except Exception as exc:
            logger.exception("[technicals] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
