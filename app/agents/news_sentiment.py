from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.tools.macro import get_geopolitical_news
from app.tools.market_data import build_market_context_text
from app.tools.news import (
    build_news_context_text,
    build_news_screening_text,
    get_articles_full_text,
)

logger = get_app_logger()

_SCREENING_SYSTEM_PROMPT = """\
You are a financial news screener. Your ONLY job is to decide which articles \
are worth reading in full before making a trading decision.

Return ONLY valid JSON — no markdown, no explanation.
"""

_SCREENING_USER_TEMPLATE = """\
Ticker: {ticker}

{screening_text}

Decide which articles (by their id= numbers) are worth reading in full.
Criteria for reading in full:
  - Breaking news DIRECTLY about this company (earnings, legal, product launch, major deal)
  - Macro event with clear direct impact on this sector/ticker
  - Analyst upgrade/downgrade or price-target change
  - Earnings guidance revision or management commentary
  
Skip: "3 Reasons to Buy"-style commentary, low-tier opinion pieces, vague macro noise \
with no clear link to {ticker}, anything from tier3 sources with a GENERIC headline.

Return JSON:
{{
  "read_full": [<list of integer id values, max 3>],
  "reason": "<one sentence why these are worth reading, or 'all noise' if none>"
}}
"""

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
  "reasoning": "<2-3 sentence summary emphasizing BREAKING/RECENT news>",
  "read_full_used": <true if you read any full articles, false otherwise>
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

_MAX_FULL_ARTICLES = 3


class NewsSentimentAgent(BaseAgent):
    """Analyzes recent news events from the DB for a specific ticker.

    Uses a two-pass approach when high-signal articles are present:
      Pass 1 (screening): agent reviews headlines and decides which to read in full.
      Pass 2 (analysis):  agent re-analyzes with full article text injected.
    Pass 2 is skipped if the screener finds nothing worth reading (saves one LLM call).
    """

    name = "news_sentiment"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        try:
            as_of = (context or {}).get("as_of")
            last_run_at = (context or {}).get("last_agent_run_at")  # set by LiveTradingService
            allowed_sources = (context or {}).get("allowed_sources")

            # ── Build geo / sentiment blocks (shared across both passes) ──
            geo_block, sentiment_block = self._build_aux_blocks(session, ticker, as_of, context)

            # ── Pass 1: screening — which articles are worth reading in full? ──
            # In live mode, `since=last_run_at` ensures screener focuses on NEW articles only.
            screening_text, raw_news = build_news_screening_text(
                session,
                ticker,
                lookback_hours=336,
                as_of=as_of,
                since=last_run_at,
                allowed_sources=allowed_sources,
            )

            # Count genuinely new articles (unseen since last run)
            new_count = sum(
                1 for item in raw_news
                if not last_run_at or (item.get("ingested_at") and
                   item["ingested_at"] >= last_run_at.isoformat())
            ) if last_run_at else len(raw_news)

            expanded_articles: dict[int, dict] = {}
            if raw_news:
                screen_prompt = _SCREENING_USER_TEMPLATE.format(
                    ticker=ticker,
                    screening_text=screening_text,
                )
                screen_raw = self._call_llm(
                    _SCREENING_SYSTEM_PROMPT, screen_prompt, response_format="json"
                )
                screen_parsed = self._parse_json_response(screen_raw)
                if screen_parsed:
                    requested_ids = screen_parsed.get("read_full", [])
                    reason = screen_parsed.get("reason", "")
                    if requested_ids:
                        # Cap at _MAX_FULL_ARTICLES to prevent token explosion
                        capped = [int(i) for i in requested_ids[:_MAX_FULL_ARTICLES]]
                        expanded_articles = get_articles_full_text(session, capped)
                        logger.info(
                            "[news_sentiment] %s screener requested %d full articles "
                            "(ids=%s, new_count=%d): %s",
                            ticker, len(capped), capped, new_count, reason,
                        )
                    else:
                        logger.debug(
                            "[news_sentiment] %s screener: all noise (new_count=%d) — %s",
                            ticker, new_count, reason,
                        )

            # ── Pass 2: full analysis (with or without expanded full text) ──
            news_context = build_news_context_text(
                session, ticker,
                lookback_hours=336, as_of=as_of,
                expanded_articles=expanded_articles or None,
                allowed_sources=allowed_sources,
            )
            market_ctx = build_market_context_text(session, ticker, as_of=as_of)
            combined = f"{news_context}\n\n{market_ctx}"

            perf_ctx = self._get_performance_context(context)
            if perf_ctx:
                combined = f"{combined}\n\n{perf_ctx}"

            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker,
                news_context=combined,
                geo_block=geo_block,
                sentiment_block=sentiment_block,
            )

            raw = self._call_llm(
                self._get_market_time_context(context) + _SYSTEM_PROMPT,
                user_prompt,
                response_format="json",
            )
            parsed = self._parse_json_response(raw)

            if not parsed:
                return AgentSignal.no_signal(self.name, "LLM unavailable or unparseable response")

            signal = parsed.get("signal", "HOLD").upper()
            if signal not in ("BUY", "SHORT", "HOLD"):
                signal = "HOLD"

            read_full_used = bool(parsed.get("read_full_used", False)) or bool(expanded_articles)

            return AgentSignal(
                agent_name=self.name,
                signal=signal,
                confidence=int(parsed.get("confidence", 30)),
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "sentiment": parsed.get("sentiment", "NEUTRAL"),
                    "event_strength": parsed.get("event_strength", "WEAK"),
                    "key_catalyst": parsed.get("key_catalyst", "none"),
                    "full_articles_read": list(expanded_articles.keys()),
                    "read_full_used": read_full_used,
                },
            )

        except Exception as exc:
            logger.exception("[news_sentiment] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))

    def _build_aux_blocks(
        self, session: Session, ticker: str, as_of, context: dict | None
    ) -> tuple[str, str]:
        """Build geo-political and Finnhub sentiment blocks (shared across passes)."""
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

        return geo_block, sentiment_block

    def _fetch_finnhub_sentiment(self, ticker: str) -> dict | None:
        """Fetch Finnhub aggregated news sentiment (live mode only)."""
        try:
            from app.ingestion.finnhub_client import FinnhubNewsClient
            client = FinnhubNewsClient(self.settings)
            return client.fetch_news_sentiment(ticker)
        except Exception as exc:
            logger.debug("[news_sentiment] Finnhub sentiment fetch failed for %s: %s", ticker, exc)
            return None
