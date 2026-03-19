from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.tools.fundamentals import build_fundamentals_context_text
from app.tools.market_data import build_market_context_text

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Fundamental analysis for equity trading.
You are a fundamental analyst at a hedge fund. Assess the fundamental quality
and valuation of a stock. Be decisive — take a clear directional stance when data supports it.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Analyze the following fundamental data for ticker {ticker}, then determine the fundamental signal.

{fundamentals_context}

Return a JSON object with these exact fields:
{{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "fundamental_quality": "<STRONG|MODERATE|WEAK|DETERIORATING>",
  "valuation": "<UNDERVALUED|FAIR|OVERVALUED|CANNOT_ASSESS>",
  "earnings_trend": "<ACCELERATING|STABLE|DECELERATING|MISSING>",
  "analyst_consensus": "<BULLISH|NEUTRAL|BEARISH|MIXED>",
  "reasoning": "<2-3 sentence summary>"
}}

Guidelines:
- BUY: strong/moderate fundamentals + fair/undervalued + bullish analyst consensus
- SHORT: weak/deteriorating fundamentals + overvalued + bearish analyst consensus
- HOLD: only when signals are genuinely contradictory (e.g., strong quality but extremely overvalued)
- Strong fundamentals with bullish consensus = BUY even at fair valuation
- If analysts are >60% bullish, that alone supports a BUY signal with moderate confidence
- confidence: 60-100 = clear signal, 35-60 = moderate, 0-35 = insufficient data
- If data is insufficient, return HOLD with confidence 25 and note the missing data
"""


class FundamentalsAgent(BaseAgent):
    """Analyzes fundamental metrics, analyst ratings, and earnings history."""

    name = "fundamentals"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            as_of = (context or {}).get("as_of")
            fundamentals_text = build_fundamentals_context_text(session, ticker, as_of=as_of)
            market_ctx = build_market_context_text(session, ticker, as_of=as_of)

            # If no fundamental data at all, return low-confidence NO_SIGNAL
            if "No fundamentals snapshot available" in fundamentals_text and "No recent" in fundamentals_text:
                return AgentSignal.no_signal(self.name, "No fundamentals data available yet")

            combined = f"{fundamentals_text}\n\n{market_ctx}"
            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker, fundamentals_context=combined
            )
            raw = self._call_llm(_SYSTEM_PROMPT, user_prompt, response_format="json")
            parsed = self._parse_json_response(raw)

            if not parsed:
                return AgentSignal.no_signal(self.name, "LLM unavailable or unparseable response")

            signal = parsed.get("signal", "HOLD").upper()
            if signal not in ("BUY", "SHORT", "HOLD"):
                signal = "HOLD"

            return AgentSignal(
                agent_name=self.name,
                signal=signal,
                confidence=int(parsed.get("confidence", 30)),
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "fundamental_quality": parsed.get("fundamental_quality", "MODERATE"),
                    "valuation": parsed.get("valuation", "CANNOT_ASSESS"),
                    "earnings_trend": parsed.get("earnings_trend", "STABLE"),
                    "analyst_consensus": parsed.get("analyst_consensus", "NEUTRAL"),
                },
            )

        except Exception as exc:
            logger.exception("[fundamentals] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
