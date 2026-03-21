from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.tools.fundamentals import build_fundamentals_context_text
from app.tools.market_data import build_market_context_text

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Fundamental analysis for equity trading — with VALUATION DISCIPLINE.
You are a value-oriented fundamental analyst at a hedge fund. You assess both quality
AND valuation. A great company can be a BAD trade if it's overvalued. A mediocre company
can be a GOOD trade if it's cheap. Always consider price relative to fundamentals.
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
- BUY: undervalued or fair valuation WITH strong/accelerating fundamentals, OR price dropped >5% with stable fundamentals (value opportunity)
- SHORT: overvalued (P/E>30) OR deteriorating fundamentals OR decelerating earnings OR analyst downgrades
- HOLD: ONLY when you genuinely cannot lean either way — this should be rare (<20% of cases)
- CRITICAL: High P/E (>25), high P/B (>8), or low earnings yield = lean SHORT, not HOLD
- Strong fundamentals at OVERVALUED levels = SHORT (the market will correct)
- Analyst consensus alone is NOT sufficient — analysts are often late and herd-like
- If recent price is near 52-week highs with average fundamentals = SHORT
- If recent price dropped significantly with strong fundamentals = BUY (value opportunity)
- When in doubt between HOLD and a direction, CHOOSE THE DIRECTION with lower confidence
- confidence: 60-100 = clear, 35-60 = moderate, 0-35 = insufficient data
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

            perf_ctx = self._get_performance_context(context)
            if perf_ctx:
                combined = f"{combined}\n\n{perf_ctx}"

            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker, fundamentals_context=combined
            )
            raw = self._call_llm(self._get_market_time_context(context) + _SYSTEM_PROMPT, user_prompt, response_format="json")
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
