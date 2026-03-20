from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.tools.market_data import build_market_context_text
from app.tools.news import build_news_context_text

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: News sentiment analysis for equity trading.
You are an expert financial news analyst. Assess recent news and events for a
specific stock ticker and determine their near-term directional impact.
Be decisive — if news leans bullish or bearish, commit to a directional signal.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Analyze the following recent news and events for ticker {ticker}, then determine the sentiment signal.

{news_context}

Return a JSON object with these exact fields:
{{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "sentiment": "<STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH>",
  "event_strength": "<STRONG|MODERATE|WEAK|NOISE>",
  "key_catalyst": "<1 sentence describing the most impactful event, or 'none'>",
  "reasoning": "<2-3 sentence summary of news impact>"
}}

Guidelines:
- BUY if recent news is bullish: earnings beat, positive guidance, deal announcement, upgrades, sector tailwinds, insider buying, buyback
- SHORT if recent news is bearish: earnings miss, negative guidance, fraud/legal, downgrades, sector headwinds, layoffs, revenue decline, tariff risk, competitive threat
- HOLD only if there is truly NO relevant news at all (zero articles). If any news exists, pick a direction!
- Even moderately positive/negative news should result in BUY/SHORT with moderate confidence (40-60)
- Multiple articles in the same direction → HIGH confidence (70+)
- confidence 60-100 = strong/clear signal, 30-60 = moderate signal, 0-30 = weak/absent
- Do NOT default to HOLD just because news is a few days old — news from the past 2 weeks is still actionable
- When in doubt between HOLD and a direction, CHOOSE THE DIRECTION with lower confidence
"""


class NewsSentimentAgent(BaseAgent):
    """Analyzes recent news events from the DB for a specific ticker."""

    name = "news_sentiment"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            as_of = (context or {}).get("as_of")
            news_text = build_news_context_text(session, ticker, lookback_hours=336, as_of=as_of)
            market_ctx = build_market_context_text(session, ticker, as_of=as_of)
            combined = f"{news_text}\n\n{market_ctx}"

            perf_ctx = self._get_performance_context(context)
            if perf_ctx:
                combined = f"{combined}\n\n{perf_ctx}"

            user_prompt = _USER_PROMPT_TEMPLATE.format(ticker=ticker, news_context=combined)

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
                    "sentiment": parsed.get("sentiment", "NEUTRAL"),
                    "event_strength": parsed.get("event_strength", "WEAK"),
                    "key_catalyst": parsed.get("key_catalyst", "none"),
                },
            )

        except Exception as exc:
            logger.exception("[news_sentiment] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
