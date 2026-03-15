from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.tools.macro import build_macro_context_text
from app.tools.market_data import build_market_context_text

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Macroeconomic analysis for equities trading.
Assess the current macro environment and its likely near-term impact on US equities.
Focus on actionable, data-driven conclusions. Be concise and precise.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Analyze the following macroeconomic data and news, then determine the current macro stance.

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
- BUY when macro supports equities: falling rates, strong growth, low VIX, positive sentiment
- SHORT when macro threatens equities: rate hikes, recession signals, high VIX, negative sentiment
- HOLD when signals are mixed or uncertain
- confidence reflects how clearly the data supports your view (80-100 = very clear, 40-60 = mixed)
"""


class MacroAnalystAgent(BaseAgent):
    """Analyzes macro environment using FRED indicators and macro news."""

    name = "macro_analyst"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            macro_text = build_macro_context_text(session)
            market_ctx = build_market_context_text(session, ticker)

            # Fallback if no FRED data available
            if "No recent" in macro_text and "N/A" in macro_text:
                logger.debug("[macro_analyst] No FRED data available, returning NO_SIGNAL")
                return AgentSignal.no_signal(self.name, "No FRED data available yet")

            combined_context = f"{macro_text}\n\n{market_ctx}"
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
