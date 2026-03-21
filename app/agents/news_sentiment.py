from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.tools.macro import get_geopolitical_news
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
Analyze the following news and events for ticker {ticker}.

News recency labels guide how to weight information:
  🔴 BREAKING  = within 24h  → HIGHEST weight, likely still moving the stock
  🟡 RECENT    = 1-3 days   → HIGH weight, market is still absorbing
  🟢 THIS WEEK = 3-7 days   → MEDIUM weight, partially priced in
  ⚪ OLDER     = 7-14 days  → BACKGROUND context only

{news_context}
{geo_block}
{sentiment_block}
Return a JSON object with these exact fields:
{{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "sentiment": "<STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH>",
  "event_strength": "<STRONG|MODERATE|WEAK|NOISE>",
  "key_catalyst": "<1 sentence describing the most impactful event, or 'none'>",
  "reasoning": "<2-3 sentence summary emphasizing BREAKING/RECENT news>"
}}

Guidelines:
- Weight 🔴/🟡 news heavily. 🟢/⚪ is background context.
- BUY if net sentiment is bullish: earnings beat, positive guidance, upgrades, buyback, deal win, tariff relief
- SHORT if net sentiment is bearish: earnings miss, downgrades, fraud/legal, revenue decline, tariff risk, competitive threat
- HOLD ONLY if there are truly NO articles at all (no labels visible in context).
  If ANY recent news exists, pick a direction!
- Multiple 🔴/🟡 articles in same direction → confidence 70+
- Conflicting signals between BREAKING and OLDER → trust the newer news
- When in doubt between HOLD and a direction, CHOOSE THE DIRECTION with lower confidence

GEOPOLITICAL EVENTS — how to apply to this ticker:
- Active war/military conflict → assess supply-chain, energy, sentiment impact on THIS stock
- New tariffs/trade war → SHORT if this company imports heavily; assess pass-through ability
- Sanctions/export bans → SHORT if this company's supply chain or markets are directly affected
- Oil shock (war/OPEC) → SHORT energy-intensive cos; BUY oil majors (XOM/CVX/COP)
- Chip export controls → SHORT semiconductor companies (NVDA/AMD/INTC/QCOM/AVGO)
- Ceasefire/deal → relief rally; consider BUY if the company was SHORT on geopolitical risk
- If the geo event has NO clear direct pathway to this company → treat as NOISE, lower confidence
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

            # Geopolitical events block (wars, tariffs, sanctions) — affects sector/ticker
            geo_events = get_geopolitical_news(session, lookback_hours=120, limit=8, as_of=as_of)
            geo_block = ""
            if geo_events:
                lines = ["=== GEOPOLITICAL & POLICY EVENTS (last 5 days) ==="]
                lines.append("(Assess impact on THIS ticker specifically — see prompt guidelines)")
                for ev in geo_events[:6]:
                    pub = ev["published_at"][:10]
                    snippet = ev["body_snippet"][:180].replace("\n", " ")
                    lines.append(f"  [{pub}][{ev['source']}] {ev['title']}")
                    if snippet and snippet.strip() != ev["title"].strip():
                        lines.append(f"    → {snippet}")
                geo_block = "\n".join(lines) + "\n"

            # Finnhub news-sentiment scores (live mode only — skip when as_of is set)
            sentiment_block = ""
            if not as_of:
                scores = self._fetch_finnhub_sentiment(ticker)
                if scores:
                    sentiment_block = (
                        f"\n[FINNHUB AGGREGATED SENTIMENT for {ticker}]\n"
                        f"  Bullish articles: {scores['bullish_pct']}% | "
                        f"Bearish articles: {scores['bearish_pct']}%\n"
                        f"  Sentiment score: {scores['sentiment_score']:+.3f} "
                        f"(+1=max bullish, -1=max bearish)\n"
                        f"  Buzz score: {scores['buzz_score']:.3f} | "
                        f"Articles this week: {scores['articles_this_week']}\n"
                    )

            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker,
                news_context=combined,
                geo_block=geo_block,
                sentiment_block=sentiment_block,
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
                    "sentiment": parsed.get("sentiment", "NEUTRAL"),
                    "event_strength": parsed.get("event_strength", "WEAK"),
                    "key_catalyst": parsed.get("key_catalyst", "none"),
                },
            )

        except Exception as exc:
            logger.exception("[news_sentiment] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))

    def _fetch_finnhub_sentiment(self, ticker: str) -> dict | None:
        """Fetch Finnhub aggregated news sentiment (live mode only)."""
        try:
            from app.ingestion.finnhub_client import FinnhubNewsClient
            client = FinnhubNewsClient(self.settings)
            return client.fetch_news_sentiment(ticker)
        except Exception as exc:
            logger.debug("[news_sentiment] Finnhub sentiment fetch failed for %s: %s", ticker, exc)
            return None

