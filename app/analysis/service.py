from __future__ import annotations

import json
import re
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


class AnalysisService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        # Finnhub supplemental data cache: key → (data, expire_ts)
        self._fh_cache: dict[str, tuple[Any, float]] = {}

    def _llm_enabled(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_model)

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

    def _build_evidence_payload(self, session: Session | None, event: Event) -> list[dict[str, Any]]:
        if session is None or event.id is None:
            return []

        rows = session.execute(
            select(EventEvidence, RawItem)
            .join(RawItem, RawItem.id == EventEvidence.raw_item_id, isouter=True)
            .where(EventEvidence.event_id == event.id)
            .order_by(EventEvidence.id.asc())
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
                "Output JSON only.",
                "direction must be exactly one of: UP, DOWN, NEUTRAL.",
                "Do not output trading actions (BUY/SELL/SHORT/HOLD).",
                "THIS IS A SHORT-TERM SIGNAL (next 1-4 hours). Judge the IMMEDIATE price reaction to the news event, NOT the long-term fundamental outlook. A company may be bullish long-term but still drop short-term on bad news.",
                "Primary signal: news content and event severity. Ask: will this news cause buyers or sellers to act in the next 1-4 hours?",
                "Use NEUTRAL only when the news is truly routine (e.g. minor analyst note, no surprise) or evidence is contradictory.",
                "Secondary signals (use only as tie-breakers when news signal is ambiguous):",
                "  - tech_signal: 'buy' supports UP, 'sell' supports DOWN",
                "  - relative_strength_vs_spy_pct: if ticker is already outperforming SPY today, UP news has more momentum",
                "  - earnings_context: positive surprise_pct supports UP, negative supports DOWN",
                "  - support_resistance: price within 1% of resistance reduces upside; within 1% of support reduces downside",
                "Do NOT let long-term bullish fundamentals override a clearly negative short-term news event.",
            ],
            "output_schema": {
                "direction": "UP|DOWN|NEUTRAL",
                "term": "SHORT|MID|LONG",
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
            "direction(UP|DOWN|NEUTRAL), term(SHORT|MID|LONG), ticker(optional), rationale(one sentence)."
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

        with httpx.Client(timeout=20.0) as client:
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
            "reason": reason,
        }

    def _fallback(self, event: Event) -> dict:
        return {
            "action": fallback_action(event.event_type),
            "ticker": event.tickers[0] if event.tickers else "",
            "horizon_min": self.settings.default_horizon_min,
            "horizon_profile": None,
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
            for _ in range(3):
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
