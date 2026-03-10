from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.analysis.rules_fallback import fallback_action
from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import Bar1m, Event, EventEvidence, RawItem
from app.schemas.types import TradeSignal

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_FINNHUB_CACHE_TTL_S = 3600  # 1 hour cache for Finnhub supplemental data
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class AnalysisService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._quality_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        # Finnhub supplemental data cache: key → (data, expire_ts)
        self._fh_cache: dict[str, tuple[Any, float]] = {}

    def _llm_enabled(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_model)

    def _llm_retry_delay(self, attempt: int) -> float:
        base = max(0.0, float(getattr(self.settings, "llm_retry_backoff_seconds", 1.5)))
        multiplier = max(1.0, float(getattr(self.settings, "llm_retry_backoff_multiplier", 1.8)))
        cap = max(0.0, float(getattr(self.settings, "llm_retry_max_delay_seconds", 12.0)))
        delay = base * (multiplier ** max(0, attempt))
        return min(delay, cap) if cap > 0 else delay

    def _llm_endpoint(self) -> str:
        base = self.settings.llm_base_url.strip()
        if base.endswith("/v1/chat/completions") or base.endswith("/chat/completions"):
            return base
        return f"{base.rstrip('/')}/v1/chat/completions"

    def _llm_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        return headers

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Invalid LLM response: choices missing")

        message = choices[0].get("message", {})
        content = message.get("content")

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
            content = "\n".join(parts).strip()
        elif isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)

        if not isinstance(content, str):
            raise ValueError("Invalid LLM response: content missing")
        return content.strip()

    @staticmethod
    def _parse_content_json(content: str) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("Empty LLM content")

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fence_match:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed

        raise ValueError("LLM content does not contain a valid JSON object")

    @staticmethod
    def _normalize_horizon_profile(parsed: dict[str, Any]) -> str | None:
        raw = (
            parsed.get("term")
            or parsed.get("horizon_profile")
            or parsed.get("term_profile")
            or parsed.get("horizon_term")
        )
        if not raw:
            return None
        key = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
        alias = {
            "SHORT": "SHORT",
            "SHORT_TERM": "SHORT",
            "INTRADAY": "SHORT",
            "SCALP": "SHORT",
            "MID": "MID",
            "MEDIUM": "MID",
            "MEDIUM_TERM": "MID",
            "SWING": "MID",
            "LONG": "LONG",
            "LONG_TERM": "LONG",
            "POSITION": "LONG",
        }
        return alias.get(key)

    @staticmethod
    def _normalize_position_pct(parsed: dict[str, Any]) -> float | None:
        raw = parsed.get("position_pct")
        if raw is None:
            raw = parsed.get("position_size_pct")
        if raw is None:
            raw = parsed.get("size_pct")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None

        if value > 1.0 and value <= 100.0:
            value = value / 100.0
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    def _profile_horizon_min(self, profile: str | None) -> int:
        if profile == "SHORT":
            return self.settings.term_short_horizon_min
        if profile == "MID":
            return self.settings.term_mid_horizon_min
        if profile == "LONG":
            return self.settings.term_long_horizon_min
        return self.settings.default_horizon_min

    @staticmethod
    def _normalize_direction(parsed: dict[str, Any], fallback_direction: str) -> str:
        raw_direction = str(parsed.get("direction", "")).strip().upper().replace("-", "_")
        alias = {
            "UP": "UP",
            "DOWN": "DOWN",
            "NEUTRAL": "NEUTRAL",
            "POSITIVE": "UP",
            "NEGATIVE": "DOWN",
            "BULLISH": "UP",
            "BEARISH": "DOWN",
        }
        direction = alias.get(raw_direction)
        if direction:
            return direction

        sentiment = str(parsed.get("sentiment", "")).strip().upper().replace("-", "_")
        if sentiment in {"POSITIVE", "BULLISH"}:
            return "UP"
        if sentiment in {"NEGATIVE", "BEARISH"}:
            return "DOWN"
        if sentiment == "NEUTRAL":
            return "NEUTRAL"

        return fallback_direction

    @staticmethod
    def _event_prior_direction(event_type: str) -> str:
        fallback = fallback_action(event_type)
        if fallback == "BUY":
            return "UP"
        if fallback == "SHORT":
            return "DOWN"
        return "NEUTRAL"

    # Headline patterns that indicate generic market-round-up articles with no directional signal
    _NOISE_TITLE_PATTERNS = re.compile(
        r"\b(trending tickers|market movers|early movers|morning movers|benzinga market summary"
        r"|top stocks|stocks to watch|notable calls|analyst upgrade|analyst downgrade"
        r"|should you invest|is .{3,40} a (good|bad)|investors could be concerned)\b",
        re.IGNORECASE,
    )

    def _build_evidence_payload(self, session: Session | None, event: Event) -> list[dict[str, Any]]:
        if session is None or event.id is None:
            return []

        rows = session.execute(
            select(EventEvidence, RawItem)
            .join(RawItem, RawItem.id == EventEvidence.raw_item_id, isouter=True)
            .where(EventEvidence.event_id == event.id)
            .order_by(EventEvidence.source_tier.asc(), EventEvidence.id.asc())  # best sources first
            .limit(8)
        ).all()

        max_chars_per_item = 20_000
        max_total_chars = 100_000
        used_chars = 0
        evidence_payload: list[dict[str, Any]] = []

        for evidence, raw in rows:
            title = (raw.title if raw and raw.title else evidence.summary) or ""
            full_text = (raw.body if raw and raw.body else evidence.summary) or ""
            full_text = full_text.strip()
            if not full_text:
                full_text = title.strip()
            if not full_text:
                continue

            # Quality filter: skip very short articles (market summaries, ticker-mention-only entries)
            # A <200 char body rarely contains enough specific information for directional signal.
            if len(full_text) < 200:
                continue

            # Skip generic market-round-up headlines (no directional signal)
            if title and self._NOISE_TITLE_PATTERNS.search(title):
                continue

            original_len = len(full_text)
            if original_len > max_chars_per_item:
                full_text = full_text[:max_chars_per_item]
            if used_chars + len(full_text) > max_total_chars:
                remain = max_total_chars - used_chars
                if remain <= 0:
                    break
                full_text = full_text[:remain]

            if not full_text:
                break

            used_chars += len(full_text)
            evidence_payload.append(
                {
                    "source": evidence.source,
                    "source_tier": evidence.source_tier,
                    "url": evidence.url,
                    "published_at": raw.published_at.isoformat() if raw and raw.published_at else None,
                    "title": title,
                    "full_text": full_text,
                    "text_truncated": original_len > len(full_text),
                }
            )

        return evidence_payload

    @staticmethod
    def _pct_change(base: float | None, current: float | None) -> float | None:
        if base is None or current is None or abs(base) < 1e-9:
            return None
        return (current - base) / base

    def _fh_get(self, path: str, params: dict) -> Any | None:
        """Cached Finnhub GET. Returns parsed JSON or None on error."""
        if not self.settings.finnhub_api_key:
            return None
        cache_key = path + str(sorted(params.items()))
        now = datetime.now(tz=timezone.utc).timestamp()
        cached = self._fh_cache.get(cache_key)
        if cached and now < cached[1]:
            return cached[0]
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{_FINNHUB_BASE}{path}",
                    params={**params, "token": self.settings.finnhub_api_key},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                self._fh_cache[cache_key] = (data, now + _FINNHUB_CACHE_TTL_S)
                return data
        except Exception:
            return None

    def _finnhub_earnings_context(self, ticker: str) -> dict | None:
        """Get last 4 EPS actuals/estimates/surprises from Finnhub."""
        data = self._fh_get("/stock/earnings", {"symbol": ticker, "limit": 4})
        if not data or not isinstance(data, list):
            return None
        quarters = []
        for q in data:
            actual = q.get("actual")
            estimate = q.get("estimate")
            surprise_pct = q.get("surprisePercent")
            if actual is None and estimate is None:
                continue
            quarters.append({
                "period": q.get("period"),
                "actual": actual,
                "estimate": estimate,
                "surprise_pct": round(surprise_pct, 2) if surprise_pct is not None else None,
            })
        if not quarters:
            return None
        last = quarters[0]
        return {
            "last_actual": last.get("actual"),
            "last_estimate": last.get("estimate"),
            "last_surprise_pct": last.get("surprise_pct"),
            "quarters": quarters,
        }

    def _finnhub_tech_signal(self, ticker: str) -> dict | None:
        """Get aggregate technical analysis signal from Finnhub."""
        data = self._fh_get("/scan/technical-indicator", {"symbol": ticker, "resolution": "D"})
        if not data or not isinstance(data, dict):
            return None
        ta = data.get("technicalAnalysis", {})
        trend = data.get("trend", {})
        signal = ta.get("signal")
        count = ta.get("count", {})
        if not signal:
            return None
        return {
            "signal": signal,
            "buy_count": count.get("buy"),
            "neutral_count": count.get("neutral"),
            "sell_count": count.get("sell"),
            "adx": round(trend.get("adx", 0), 2) if trend.get("adx") is not None else None,
            "trending": trend.get("trending"),
        }

    def _finnhub_analyst_consensus(self, ticker: str) -> dict | None:
        """Get analyst recommendation trend from Finnhub (last 2 periods)."""
        data = self._fh_get("/stock/recommendation", {"symbol": ticker})
        if not data or not isinstance(data, list) or not data:
            return None
        # Sort by period descending (most recent first)
        data_sorted = sorted(data, key=lambda x: x.get("period", ""), reverse=True)
        latest = data_sorted[0]
        strong_buy = latest.get("strongBuy", 0)
        buy = latest.get("buy", 0)
        hold = latest.get("hold", 0)
        sell = latest.get("sell", 0)
        strong_sell = latest.get("strongSell", 0)
        total = strong_buy + buy + hold + sell + strong_sell
        if total == 0:
            return None
        bullish = strong_buy + buy
        bearish = sell + strong_sell
        score = (bullish - bearish) / total  # range -1 to +1
        result = {
            "period": latest.get("period"),
            "strong_buy": strong_buy,
            "buy": buy,
            "hold": hold,
            "sell": sell,
            "strong_sell": strong_sell,
            "total_analysts": total,
            "consensus_score": round(score, 3),  # >0 bullish, <0 bearish
            "consensus": "BULLISH" if score > 0.2 else "BEARISH" if score < -0.2 else "NEUTRAL",
        }
        return result

    def _finnhub_support_resistance(self, ticker: str, current_price: float | None) -> dict | None:
        """Get support/resistance levels and proximity to current price."""
        data = self._fh_get("/scan/support-resistance", {"symbol": ticker, "resolution": "D"})
        if not data or not isinstance(data, dict):
            return None
        levels = sorted(data.get("levels", []))
        if not levels:
            return None
        result: dict[str, Any] = {"levels": [round(l, 2) for l in levels]}
        if current_price and current_price > 0:
            supports = [l for l in levels if l < current_price]
            resistances = [l for l in levels if l > current_price]
            nearest_support = supports[-1] if supports else None
            nearest_resistance = resistances[0] if resistances else None
            result["nearest_support"] = round(nearest_support, 2) if nearest_support else None
            result["nearest_resistance"] = round(nearest_resistance, 2) if nearest_resistance else None
            if nearest_resistance:
                result["pct_to_resistance"] = round((nearest_resistance - current_price) / current_price * 100, 2)
            if nearest_support:
                result["pct_from_support"] = round((current_price - nearest_support) / current_price * 100, 2)
        return result

    def _event_market_features(self, session: Session | None, event: Event) -> dict[str, Any]:
        if session is None or not event.tickers:
            return {}

        event_ts = ensure_utc(event.event_time)
        ticker = str(event.tickers[0]).upper()
        day_open = event_ts.replace(hour=14, minute=30, second=0, microsecond=0)
        if event_ts < day_open:
            day_open = day_open - timedelta(days=1)
        day_end = day_open + timedelta(days=1)

        def first_bar(sym: str) -> Bar1m | None:
            return session.execute(
                select(Bar1m)
                .where(and_(Bar1m.ticker == sym, Bar1m.ts >= day_open, Bar1m.ts < day_end))
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).scalar_one_or_none()

        def last_bar_until_event(sym: str) -> Bar1m | None:
            bar = session.execute(
                select(Bar1m)
                .where(and_(Bar1m.ticker == sym, Bar1m.ts >= day_open, Bar1m.ts <= event_ts))
                .order_by(Bar1m.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            if bar:
                return bar
            return session.execute(
                select(Bar1m)
                .where(and_(Bar1m.ticker == sym, Bar1m.ts > event_ts, Bar1m.ts < day_end))
                .order_by(Bar1m.ts.asc())
                .limit(1)
            ).scalar_one_or_none()

        def prev_close(sym: str) -> Bar1m | None:
            return session.execute(
                select(Bar1m)
                .where(and_(Bar1m.ticker == sym, Bar1m.ts < day_open))
                .order_by(Bar1m.ts.desc())
                .limit(1)
            ).scalar_one_or_none()

        ticker_open = first_bar(ticker)
        ticker_now = last_bar_until_event(ticker)
        ticker_prev = prev_close(ticker)

        spy_open = first_bar("SPY")
        spy_now = last_bar_until_event("SPY")
        spy_prev = prev_close("SPY")

        ticker_intraday_ret = self._pct_change(
            float(ticker_open.open) if ticker_open else None,
            float(ticker_now.close) if ticker_now else None,
        )
        spy_intraday_ret = self._pct_change(
            float(spy_open.open) if spy_open else None,
            float(spy_now.close) if spy_now else None,
        )

        current_price = float(ticker_now.close) if ticker_now else None
        earnings_ctx = self._finnhub_earnings_context(ticker)
        tech_signal = self._finnhub_tech_signal(ticker)
        sr_levels = self._finnhub_support_resistance(ticker, current_price)
        analyst_consensus = self._finnhub_analyst_consensus(ticker)

        features: dict[str, Any] = {
            "ticker": ticker,
            "event_time_utc": event_ts.isoformat(),
            "session_open_utc": day_open.isoformat(),
            "ticker_intraday_return_pct": ticker_intraday_ret,
            "spy_intraday_return_pct": spy_intraday_ret,
            "relative_strength_vs_spy_pct": (
                (ticker_intraday_ret - spy_intraday_ret)
                if ticker_intraday_ret is not None and spy_intraday_ret is not None
                else None
            ),
            "ticker_since_prev_close_pct": self._pct_change(
                float(ticker_prev.close) if ticker_prev else None,
                float(ticker_now.close) if ticker_now else None,
            ),
            "spy_since_prev_close_pct": self._pct_change(
                float(spy_prev.close) if spy_prev else None,
                float(spy_now.close) if spy_now else None,
            ),
            "ticker_price_points": {
                "open": float(ticker_open.open) if ticker_open else None,
                "event_close": float(ticker_now.close) if ticker_now else None,
            },
            "spy_price_points": {
                "open": float(spy_open.open) if spy_open else None,
                "event_close": float(spy_now.close) if spy_now else None,
            },
        }
        if earnings_ctx:
            features["earnings_context"] = earnings_ctx
        if tech_signal:
            features["tech_signal"] = tech_signal
        if sr_levels:
            features["support_resistance"] = sr_levels
        if analyst_consensus:
            features["analyst_consensus"] = analyst_consensus

        # ── Macro market regime: SPY 20-day trend ─────────────────────────────
        # Fetch the SPY bar ~20 trading days ago (≈28 calendar days) and compute
        # cumulative return up to the event time. Tells the LLM whether the broad
        # market has been in a risk-on or risk-off regime recently.
        spy_20d_ago_ts = event_ts - timedelta(days=28)
        spy_20d_bar = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == "SPY", Bar1m.ts >= spy_20d_ago_ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()

        if spy_20d_bar and spy_now:
            spy_20d_return = self._pct_change(
                float(spy_20d_bar.close), float(spy_now.close)
            )
            if spy_20d_return is not None:
                if spy_20d_return >= 0.03:
                    regime = "BULL"
                elif spy_20d_return <= -0.03:
                    regime = "BEAR"
                else:
                    regime = "NEUTRAL"
                features["macro_market_regime"] = {
                    "spy_20d_return_pct": round(spy_20d_return * 100, 2),
                    "regime": regime,
                    "note": "SPY cumulative return over last ~20 trading days",
                }

        return features

    def _llm_extract(self, event: Event, session: Session | None = None) -> dict:
        prior_direction = self._event_prior_direction(event.event_type)
        evidence_payload = self._build_evidence_payload(session, event)
        market_features = self._event_market_features(session, event)
        prompt = {
            "task": "Classify likely near-term direction impact for the mentioned US equity event.",
            "event": {
                "event_id": event.id,
                "event_time": event.event_time.isoformat() if event.event_time else None,
                "event_type": event.event_type,
                "tickers": event.tickers,
                "severity": event.severity,
                "confidence": event.confidence,
                "summary": event.summary,
            },
            "prior_hint": {
                "event_type_direction_bias": prior_direction,
            },
            "market_features": market_features,
            "evidence": evidence_payload,
            "rules": [
                "RELEVANCE CHECK (do this first): Before predicting direction, verify the articles are actually about a company-specific event for the named ticker(s). If the articles are about macro/broad-market topics (central bank policy, government shutdown, broad sector ETFs, another company entirely) with only a passing mention of the ticker, output NEUTRAL with rationale 'article_not_ticker_specific'.",
                "MERGER/ACQUISITION RULE: If event_type is 'merger_acquisition' and the ticker is the TARGET (being acquired), direction is almost always UP (acquisition premium). Only choose DOWN if the ticker is the ACQUIRER paying a very high premium with clear negative market reaction evidence.",
                "Output JSON only.",
                "direction must be exactly one of: UP, DOWN, NEUTRAL.",
                "Do not output trading actions (BUY/SELL/SHORT/HOLD).",
                "If direction is UP or DOWN, output position_pct in [0,1] as the fraction of max allowed position to use. Use higher size only on very high-conviction, ticker-specific events. If direction is NEUTRAL, set position_pct to 0.",
                "THIS IS A SHORT-TERM SIGNAL (next 1-4 hours). Judge the IMMEDIATE price reaction to the news event, NOT the long-term fundamental outlook. A company may be bullish long-term but still drop short-term on bad news.",
                "Primary signal: news content and event severity. Ask: will this news cause buyers or sellers to act in the next 1-4 hours?",
                "Use NEUTRAL only when the news is truly routine (e.g. minor analyst note, no surprise) or evidence is contradictory.",
                "Secondary signals (use only as tie-breakers when news signal is ambiguous):",
                "  - tech_signal: 'buy' supports UP, 'sell' supports DOWN",
                "  - relative_strength_vs_spy_pct: if ticker is already outperforming SPY today, UP news has more momentum",
                "  - earnings_context: positive surprise_pct supports UP, negative supports DOWN",
                "  - support_resistance: price within 1% of resistance reduces upside; within 1% of support reduces downside",
                "  - analyst_consensus: BULLISH (consensus_score>0.2) is a mild UP tailwind; BEARISH is a mild DOWN tailwind — but NEVER override a strong news signal",
                "  - macro_market_regime: Use as a DIRECTIONAL TILT for ambiguous signals only. In a BULL regime (spy_20d_return >= +3%), prefer UP when news is ambiguous; avoid initiating DOWN trades on weak negative signals. In a BEAR regime (spy_20d_return <= -3%), prefer DOWN when ambiguous; avoid initiating UP trades on weak positive signals. NEVER override a clear, strong, ticker-specific news signal based on macro alone.",
                "Do NOT let long-term bullish fundamentals override a clearly negative short-term news event.",
            ],
            "output_schema": {
                "direction": "UP|DOWN|NEUTRAL",
                "term": "SHORT|MID|LONG",
                "position_pct": "float in [0,1]",
                "ticker": "optional",
                "rationale": "brief string",
            },
        }
        system = (
            "You are a SHORT-TERM event-direction classifier for US equities. "
            "Your job is to predict the stock price direction in the NEXT 1-4 HOURS following a news event. "
            "This is NOT a long-term fundamental analysis — focus only on immediate market reaction. "
            "News is the PRIMARY signal. Technical indicators (tech_signal, support_resistance) and "
            "earnings context are SECONDARY tie-breakers only. "
            "Return strict JSON only with keys: "
            "direction(UP|DOWN|NEUTRAL), term(SHORT|MID|LONG), position_pct(0..1), "
            "ticker(optional), rationale(one sentence)."
        )
        base_payload = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        payloads = [
            {**base_payload, "response_format": {"type": "json_object"}},
            base_payload,
        ]
        headers = self._llm_headers()
        endpoint = self._llm_endpoint()
        last_error: Exception | None = None

        llm_timeout = max(5.0, float(getattr(self.settings, "llm_timeout_seconds", 30.0)))
        with httpx.Client(timeout=llm_timeout) as client:
            for idx, payload in enumerate(payloads):
                try:
                    response = client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    # Some gateways do not support response_format.
                    if idx == 0 and response.status_code in {400, 404, 415, 422}:
                        last_error = ValueError(f"LLM gateway rejected response_format: {response.status_code}")
                        continue
                    if response.status_code in _RETRYABLE_HTTP_STATUS:
                        response.raise_for_status()
                    response.raise_for_status()
                    body = response.json()
                    content = self._extract_content(body)
                    parsed = self._parse_content_json(content)
                    break
                except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                    last_error = exc
                    if idx == 0:
                        continue
                    raise
            else:
                if last_error:
                    raise last_error
                raise RuntimeError("LLM request failed")

        direction = self._normalize_direction(parsed, fallback_direction=prior_direction)

        direction_action = {
            "UP": "BUY",
            "DOWN": "SHORT",
            "NEUTRAL": "HOLD",
        }
        action = direction_action.get(direction, "HOLD")

        horizon_profile = self._normalize_horizon_profile(parsed)
        position_pct_suggestion = self._normalize_position_pct(parsed)
        if direction == "NEUTRAL":
            position_pct_suggestion = 0.0
        horizon_min = self.settings.default_horizon_min
        if self.settings.enable_term_management:
            if horizon_profile:
                horizon_min = self._profile_horizon_min(horizon_profile)
            else:
                horizon_raw = parsed.get("horizon_min", self.settings.default_horizon_min)
                try:
                    horizon_min = int(horizon_raw)
                except (TypeError, ValueError):
                    horizon_min = self.settings.default_horizon_min
                if horizon_min < 1:
                    horizon_min = self.settings.default_horizon_min

        reason = str(parsed.get("reason") or parsed.get("rationale") or "llm_generated").strip() or "llm_generated"

        ticker = parsed.get("ticker")
        if not ticker:
            entities = parsed.get("entities")
            if isinstance(entities, list) and entities:
                ticker = entities[0]
        ticker = str(ticker).strip().upper() if ticker else ""
        ticker = ticker or (event.tickers[0] if event.tickers else "")

        return {
            "action": action,
            "direction": direction,
            "ticker": ticker,
            "horizon_min": horizon_min,
            "horizon_profile": horizon_profile,
            "position_pct_suggestion": position_pct_suggestion,
            "reason": reason,
        }

    def _fallback(self, event: Event) -> dict:
        return {
            "action": fallback_action(event.event_type),
            "ticker": event.tickers[0] if event.tickers else "",
            "horizon_min": self.settings.default_horizon_min,
            "horizon_profile": None,
            "position_pct_suggestion": None,
            "reason": f"fallback rule for {event.event_type}",
        }

    def _llm_cache_key(self, event: Event) -> tuple[Any, ...]:
        return (
            event.id,
            event.event_type,
            tuple(event.tickers or []),
            event.severity,
            event.confidence,
            (event.summary or "")[:300],
            self.settings.llm_model,
            self.settings.llm_base_url,
            self.settings.enable_term_management,
            self.settings.term_short_horizon_min,
            self.settings.term_mid_horizon_min,
            self.settings.term_long_horizon_min,
        )

    def _quality_cache_key(self, event: Event) -> tuple[Any, ...]:
        return (
            event.id,
            event.event_type,
            tuple(event.tickers or []),
            event.severity,
            event.confidence,
            (event.summary or "")[:500],
            self.settings.llm_classifier_model,
            self.settings.llm_base_url,
        )

    def assess_event_quality(self, event: Event, session: Session | None = None) -> dict[str, Any]:
        model = (self.settings.llm_classifier_model or "").strip()
        if not self.settings.llm_base_url or not model:
            return {
                "quality": "UNKNOWN",
                "quality_score": 0,
                "reason": "quality_llm_disabled",
                "model": model,
                "error": "quality_llm_disabled",
            }

        cache_key = self._quality_cache_key(event)
        cached = self._quality_cache.get(cache_key)
        if cached:
            return dict(cached)

        evidence_payload = self._build_evidence_payload(session, event)
        if not evidence_payload:
            result = {
                "quality": "LOW",
                "quality_score": 15,
                "reason": "no_high_quality_evidence_text",
                "model": model,
            }
            self._quality_cache[cache_key] = dict(result)
            return result

        prompt = {
            "task": "Score event quality for short-term event-driven trading.",
            "event": {
                "event_id": event.id,
                "event_time": event.event_time.isoformat() if event.event_time else None,
                "event_type": event.event_type,
                "tickers": event.tickers,
                "severity": event.severity,
                "confidence": event.confidence,
                "summary": event.summary,
            },
            "evidence": evidence_payload,
            "rules": [
                "Focus on execution quality, not direction.",
                "HIGH: ticker-specific, concrete new information, at least one reliable source, actionable within next 1-4 hours.",
                "MEDIUM: partially specific or mixed evidence, maybe actionable but uncertain.",
                "LOW: routine filing, macro round-up, weak evidence, ticker mention-only, stale/duplicate, or contradictory.",
                "Routine headlines like '<ticker> filed 8-K/10-K/10-Q' without explicit negative/positive surprise should be LOW.",
                "Output strict JSON only.",
            ],
            "output_schema": {
                "quality": "HIGH|MEDIUM|LOW",
                "quality_score": "0-100 integer",
                "reason": "one sentence",
            },
        }
        system = (
            "You are an event quality gate for an event-driven US equity strategy. "
            "Judge whether the event has enough specificity and evidence quality to trade. "
            "Do not predict direction. Return strict JSON."
        )

        payloads = [
            {
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            },
            {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            },
        ]
        endpoint = self._llm_endpoint()
        headers = self._llm_headers()

        parsed: dict[str, Any] | None = None
        last_error: Exception | None = None
        try:
            llm_timeout = max(5.0, float(getattr(self.settings, "llm_timeout_seconds", 30.0)))
            with httpx.Client(timeout=llm_timeout) as client:
                max_retries = max(1, int(getattr(self.settings, "llm_max_retries", 3)))
                for idx, payload in enumerate(payloads):
                    for attempt in range(max_retries):
                        try:
                            resp = client.post(endpoint, json=payload, headers=headers)
                            if idx == 0 and resp.status_code in {400, 404, 415, 422}:
                                last_error = ValueError(f"quality model rejected response_format: {resp.status_code}")
                                break
                            if resp.status_code in _RETRYABLE_HTTP_STATUS:
                                resp.raise_for_status()
                            resp.raise_for_status()
                            parsed = self._parse_content_json(self._extract_content(resp.json()))
                            break
                        except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                            last_error = exc
                            if attempt < max_retries - 1:
                                time.sleep(self._llm_retry_delay(attempt))
                                continue
                            break
                    if parsed:
                        break
                    if idx == 0:
                        continue
        except Exception as exc:  # pragma: no cover - defensive
            last_error = exc

        if not parsed:
            result = {
                "quality": "UNKNOWN",
                "quality_score": 0,
                "reason": "quality_llm_failed",
                "model": model,
                "error": str(last_error) if last_error else "quality_llm_failed",
            }
            self._quality_cache[cache_key] = dict(result)
            return result

        quality = str(parsed.get("quality", "")).strip().upper()
        if quality not in {"HIGH", "MEDIUM", "LOW"}:
            quality = "LOW"
        raw_score = parsed.get("quality_score")
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            score = 80 if quality == "HIGH" else 55 if quality == "MEDIUM" else 25
        score = max(0, min(100, score))
        reason = str(parsed.get("reason") or "quality_scored").strip() or "quality_scored"

        result = {
            "quality": quality,
            "quality_score": score,
            "reason": reason,
            "model": model,
        }
        self._quality_cache[cache_key] = dict(result)
        return result

    def event_to_signal(self, event: Event, session: Session | None = None) -> TradeSignal | None:
        if not event.tickers:
            return None

        fallback_used = False
        data: dict

        if self._llm_enabled():
            data = {}
            success = False
            cache_key = self._llm_cache_key(event)
            cached = self._llm_cache.get(cache_key)
            if cached:
                data = dict(cached)
                success = True
            max_retries = max(1, int(getattr(self.settings, "llm_max_retries", 3)))
            for attempt in range(max_retries):
                if success:
                    break
                try:
                    if session is None:
                        data = self._llm_extract(event)
                    else:
                        data = self._llm_extract(event, session)
                    success = True
                    self._llm_cache[cache_key] = dict(data)
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        time.sleep(self._llm_retry_delay(attempt))
                    continue
            if not success:
                data = self._fallback(event)
                fallback_used = True
        else:
            data = self._fallback(event)
            fallback_used = True

        expires = utc_now() + timedelta(minutes=data["horizon_min"])

        return TradeSignal(
            action=data["action"],
            ticker=data["ticker"],
            confidence=event.confidence,
            horizon_min=data["horizon_min"],
            horizon_profile=data.get("horizon_profile"),
            position_pct_suggestion=data.get("position_pct_suggestion"),
            reason=data["reason"],
            expires_at=expires,
            fallback_used=fallback_used,
        )

    def pending_events(self, session: Session) -> list[Event]:
        return (
            session.query(Event)
            .filter(Event.signaled.is_(False), Event.validation_status == "VALID")
            .order_by(Event.event_time.asc())
            .all()
        )
