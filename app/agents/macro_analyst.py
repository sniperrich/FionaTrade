from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.tools.macro import build_macro_context_text
from app.tools.market_data import build_market_context_text

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Macroeconomic analysis for equities trading — SHORT-TERM focus (1-5 day horizon).
You are a macro strategist at a hedge fund focused on NEAR-TERM (1-5 trading days) equity moves.
Your job is NOT to assess long-term economic health. Instead, assess whether macro conditions
favor buying or selling equities RIGHT NOW this week.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Analyze the following macroeconomic data and news, then determine the current macro stance
for the NEXT 1-5 TRADING DAYS.

{macro_context}

Return a JSON object with these exact fields:
{{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "macro_regime": "<RISK_ON|RISK_OFF|NEUTRAL|UNCERTAIN>",
  "key_risks": ["<risk1>", "<risk2>"],
  "key_tailwinds": ["<tailwind1>"],
  "reasoning": "<2-3 sentence summary>"
}}

Guidelines:
- You are making a SHORT-TERM call, not a long-term economic forecast
- BUY: immediate catalysts favor equities — falling VIX, dovish Fed, strong data surprise, risk-on mood
- SHORT: near-term headwinds — rising VIX, hawkish surprise, geopolitical shock, risk-off sentiment
- HOLD: genuinely mixed short-term signals with no clear lean — this should be RARE
- When in doubt, pick the DIRECTION with lower confidence rather than defaulting to HOLD
- A strong economy does NOT mean BUY if markets already priced it in or if sentiment is shifting
- Rising rates, tariff threats, or geopolitical tension = lean SHORT, but only if confirmed by other indicators
- VIX < 20 = NEUTRAL (normal market conditions, do NOT treat as bearish); VIX 20-25 = lean SHORT; VIX > 25 = strong SHORT; VIX < 12 = lean BUY
- If recent price action is DOWN despite good fundamentals, consider HOLD rather than reflexively going SHORT
- confidence: 60-100 = clear, 35-60 = moderate lean, 0-35 = weak lean
"""


class MacroAnalystAgent(BaseAgent):
    """Analyzes macro environment using FRED indicators and macro news."""

    name = "macro_analyst"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            as_of = (context or {}).get("as_of")
            macro_text = build_macro_context_text(session, as_of=as_of)
            market_ctx = build_market_context_text(session, ticker, as_of=as_of)

            # Fallback if no FRED data available
            if "No recent" in macro_text and "N/A" in macro_text:
                logger.debug("[macro_analyst] No FRED data available, returning NO_SIGNAL")
                return AgentSignal.no_signal(self.name, "No FRED data available yet")

            combined_context = f"{macro_text}\n\n{market_ctx}"

            # Inject performance feedback if available
            perf_ctx = self._get_performance_context(context)
            if perf_ctx:
                combined_context = f"{combined_context}\n\n{perf_ctx}"

            user_prompt = _USER_PROMPT_TEMPLATE.format(macro_context=combined_context)
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
                confidence=int(parsed.get("confidence", 50)),
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "macro_regime": parsed.get("macro_regime", "UNCERTAIN"),
                    "key_risks": parsed.get("key_risks", []),
                    "key_tailwinds": parsed.get("key_tailwinds", []),
                },
            )

        except Exception as exc:
            logger.exception("[macro_analyst] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
