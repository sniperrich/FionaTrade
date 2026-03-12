from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.analysis.rules_fallback import fallback_action
from app.analysis.taxonomy import resolve_event_type_for_text
from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import Bar1m, EarningsCalendar, Event, EventEvidence, RawItem
from app.schemas.types import TradeSignal

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_FINNHUB_CACHE_TTL_S = 3600  # 1 hour cache for Finnhub supplemental data
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class AnalysisService:
    _NY_TZ = ZoneInfo("America/New_York")

    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._quality_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._tradeability_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._earnings_review_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
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

    _WEAK_OPINION_PATTERNS = re.compile(
        r"\b(overbought|oversold|undervalued|overvalued|valuation|price target|what it means for"
        r"|what this means for|after a strong run|worth a look|buy sell hold|is .{3,50} a buy"
        r"|should you buy|should investors|bull case|bear case|technical analysis"
        r"|momentum stock|why .{3,60} stock|shares? (?:look|looks) expensive)\b",
        re.IGNORECASE,
    )
    _PRICE_ACTION_ONLY_PATTERNS = re.compile(
        r"\b(shares? (?:rise|rises|fell|fall|falls|drop|drops|jump|jumps|gain|gains)"
        r"|stock (?:rise|rises|fell|falls|drop|drops|jump|jumps|gain|gains)"
        r"|lead[s]? dow|lead[s]? s&p|rally|sell-off)\b",
        re.IGNORECASE,
    )
    _HARD_EVENT_PATTERNS = re.compile(
        r"\b(settle[sd]?|settlement|acquire[sd]?|acquisition|merger|buyback|repurchase"
        r"|guidance|forecast|outlook|earnings|estimate|sec|doj|investigation|probe"
        r"|lawsuit|litigation|contract|award|order|penalty|fine|recall|faa|fda"
        r"|layoff|restructuring|bankrupt|chapter 11|fire|explosion|plant|factory"
        r"|shutdown|accident|fraud|material weakness|restatement|dividend|deal)\b",
        re.IGNORECASE,
    )
    _EARNINGS_POSITIVE_RE = re.compile(
        r"\b(beats? (?:estimates|expectations)|guides? above|raises? (?:guidance|forecast|outlook)"
        r"|higher sales|better than expected|strong demand|revenue (?:rose|up)|profit (?:rose|up))\b",
        re.IGNORECASE,
    )
    _EARNINGS_NEGATIVE_RE = re.compile(
        r"\b(missed? estimates|below expectations|cuts? (?:guidance|forecast|outlook)|soft demand"
        r"|weaker than expected|warned on outlook)\b",
        re.IGNORECASE,
    )
    _WEAK_OPINION_SOURCES = {
        "seekingalpha",
        "motley fool",
        "fool",
        "investorplace",
        "simplywallst",
        "simply wall st",
        "zacks",
        "barchart",
    }

    def _evidence_rows(self, session: Session | None, event: Event, limit: int = 8) -> list[dict[str, Any]]:
        if session is None or event.id is None:
            return []

        rows = session.execute(
            select(EventEvidence, RawItem)
            .join(RawItem, RawItem.id == EventEvidence.raw_item_id, isouter=True)
            .where(EventEvidence.event_id == event.id)
            .order_by(EventEvidence.source_tier.asc(), EventEvidence.id.asc())
            .limit(limit)
        ).all()

        items: list[dict[str, Any]] = []
        for evidence, raw in rows:
            title = (raw.title if raw and raw.title else evidence.summary) or ""
            full_text = (raw.body if raw and raw.body else evidence.summary) or ""
            full_text = full_text.strip()
            if not full_text:
                full_text = title.strip()
            published_at = raw.published_at.isoformat() if raw and raw.published_at else None
            if raw and raw.published_at and event.event_time:
                if ensure_utc(raw.published_at) > ensure_utc(event.event_time):
                    continue
            items.append(
                {
                    "source": evidence.source,
                    "source_tier": evidence.source_tier,
                    "url": evidence.url,
                    "published_at": published_at,
                    "title": title,
                    "full_text": full_text,
                }
            )
        return items

    def _build_evidence_payload(self, session: Session | None, event: Event) -> list[dict[str, Any]]:
        max_chars_per_item = 20_000
        max_total_chars = 100_000
        used_chars = 0
        evidence_payload: list[dict[str, Any]] = []

        for row in self._evidence_rows(session, event):
            title = row["title"]
            full_text = row["full_text"]
            if not full_text:
                continue

            # Quality filter: skip very short articles (market summaries, ticker-mention-only entries)
            # A <200 char body rarely contains enough specific information for directional signal.
            if len(full_text) < 200:
                continue

            # Skip generic market-round-up headlines (no directional signal)
            if title and self._NOISE_TITLE_PATTERNS.search(title):
                continue
            # Skip opinion / valuation / technical-commentary content.
            if title and self._WEAK_OPINION_PATTERNS.search(title):
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
                    "source": row["source"],
                    "source_tier": row["source_tier"],
                    "url": row["url"],
                    "published_at": row["published_at"],
                    "title": title,
                    "full_text": full_text,
                    "text_truncated": original_len > len(full_text),
                }
            )

        return evidence_payload

    def _tradeability_cache_key(self, event: Event) -> tuple[Any, ...]:
        return (
            event.id,
            event.event_type,
            tuple(event.tickers or []),
            tuple(event.entities or []),
            event.severity,
            event.confidence,
            (event.summary or "")[:500],
        )

    def assess_tradeability(self, event: Event, session: Session | None = None) -> dict[str, Any]:
        cache_key = self._tradeability_cache_key(event)
        cached = self._tradeability_cache.get(cache_key)
        if cached:
            return dict(cached)

        evidence_rows = self._evidence_rows(session, event)
        candidates = evidence_rows or [
            {
                "source": "",
                "source_tier": 9,
                "title": event.summary or "",
                "full_text": event.summary or "",
            }
        ]
        effective_event_type = self._analysis_event_type(event, session=session)
        ticker = str(event.tickers[0]).upper() if event.tickers else ""
        entities = [str(x).lower() for x in (event.entities or []) if x]

        opinion_hits = 0
        price_action_hits = 0
        hard_event_hits = 0
        ticker_specific_hits = 0
        unique_sources = {str(item.get("source") or "").strip().lower() for item in candidates if item.get("source")}
        strong_sources = sum(1 for item in candidates if int(item.get("source_tier") or 9) <= 1)
        weak_source_only = bool(unique_sources) and all(source in self._WEAK_OPINION_SOURCES for source in unique_sources)

        for item in candidates:
            title = str(item.get("title") or "")
            text = f"{title}\n{item.get('full_text') or ''}".lower()
            title_lower = title.lower()
            if self._WEAK_OPINION_PATTERNS.search(title) or self._WEAK_OPINION_PATTERNS.search(text):
                opinion_hits += 1
            if self._PRICE_ACTION_ONLY_PATTERNS.search(title_lower) and not self._HARD_EVENT_PATTERNS.search(text):
                price_action_hits += 1
            if self._HARD_EVENT_PATTERNS.search(text):
                hard_event_hits += 1
            mention_count = 0
            if ticker:
                mention_count += text.count(ticker.lower())
            for entity in entities[:2]:
                if len(entity) >= 4:
                    mention_count += text.count(entity)
            if mention_count >= 2 or (effective_event_type and effective_event_type != "unknown" and self._HARD_EVENT_PATTERNS.search(text)):
                ticker_specific_hits += 1

        score = 35
        score += min(max(int(event.confidence or 0), 0), 100) // 8
        score += min(max(int(event.severity or 0), 0), 100) // 10
        if strong_sources:
            score += 12
        if len(unique_sources) >= 2:
            score += 8
        if ticker_specific_hits:
            score += 10
        if hard_event_hits:
            score += 18
        if effective_event_type and effective_event_type != "unknown":
            score += 6
        if weak_source_only:
            score -= 18
        if price_action_hits:
            score -= 22
        if opinion_hits:
            score -= 35

        earnings_review = None
        summary_lower = (event.summary or "").lower()
        if session is not None and (
            effective_event_type in {"earnings_miss", "guidance_cut", "sec_earnings_release"}
            or "earnings" in summary_lower
            or "guidance" in summary_lower
            or "estimates" in summary_lower
        ):
            earnings_review = self.build_earnings_review(session, ticker, ensure_utc(event.event_time))
            if earnings_review:
                if earnings_review.get("tradeability") == "POOR":
                    score -= 25
                elif earnings_review.get("tradeability") == "MARGINAL":
                    score -= 10

        score = max(0, min(score, 100))
        reason = "tradeable"
        tradeable = True

        if opinion_hits and not hard_event_hits:
            tradeable = False
            reason = "opinion_or_technical_commentary"
        elif price_action_hits and not hard_event_hits:
            tradeable = False
            reason = "price_action_roundup"
        elif weak_source_only and effective_event_type == "unknown" and hard_event_hits == 0:
            tradeable = False
            reason = "weak_source_unknown"
        elif effective_event_type == "unknown" and hard_event_hits == 0 and len(unique_sources) < 2:
            tradeable = False
            reason = "unknown_without_hard_catalyst"
        elif earnings_review and earnings_review.get("tradeability") == "POOR":
            tradeable = False
            reason = "earnings_high_bar_risk"
        elif score < self.settings.event_tradeability_min_score:
            tradeable = False
            reason = "tradeability_score_too_low"

        result = {
            "tradeable": tradeable,
            "score": score,
            "reason": reason,
            "opinion_hits": opinion_hits,
            "price_action_hits": price_action_hits,
            "hard_event_hits": hard_event_hits,
            "ticker_specific_hits": ticker_specific_hits,
            "unique_sources": len(unique_sources),
            "strong_sources": strong_sources,
            "weak_source_only": weak_source_only,
            "earnings_review": earnings_review,
        }
        self._tradeability_cache[cache_key] = dict(result)
        return result

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

    def _finnhub_earnings_context(self, ticker: str, event_ts: datetime | None = None) -> dict | None:
        """Get trailing earnings history as-of event_ts to avoid lookahead in backtests."""
        data = self._fh_get("/stock/earnings", {"symbol": ticker, "limit": 8})
        if not data or not isinstance(data, list):
            return None
        asof_date = ensure_utc(event_ts or utc_now()).date().isoformat()
        quarters = []
        for q in data:
            period = str(q.get("period") or "")
            if period and period > asof_date:
                continue
            actual = q.get("actual")
            estimate = q.get("estimate")
            surprise_pct = q.get("surprisePercent")
            if actual is None and estimate is None:
                continue
            quarters.append(
                {
                    "period": period or None,
                    "actual": actual,
                    "estimate": estimate,
                    "surprise_pct": round(surprise_pct, 2) if surprise_pct is not None else None,
                }
            )
        if not quarters:
            return None
        last = quarters[0]
        return {
            "last_actual": last.get("actual"),
            "last_estimate": last.get("estimate"),
            "last_surprise_pct": last.get("surprise_pct"),
            "quarters": quarters[:4],
        }

    def _earnings_calendar_context(
        self,
        session: Session | None,
        ticker: str,
        event_ts: datetime,
        include_upcoming: bool = False,
    ) -> dict | None:
        asof = ensure_utc(event_ts).replace(hour=0, minute=0, second=0, microsecond=0)
        latest_past = None
        next_upcoming = None

        if session is not None:
            latest_past = session.execute(
                select(EarningsCalendar)
                .where(EarningsCalendar.symbol == ticker, EarningsCalendar.report_date <= asof)
                .order_by(EarningsCalendar.report_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if include_upcoming:
                next_upcoming = session.execute(
                    select(EarningsCalendar)
                    .where(EarningsCalendar.symbol == ticker, EarningsCalendar.report_date > asof)
                    .order_by(EarningsCalendar.report_date.asc())
                    .limit(1)
                ).scalar_one_or_none()

        trailing = self._finnhub_earnings_context(ticker, event_ts=event_ts)
        last_report = latest_past.report_date if latest_past else None
        if last_report is None and trailing:
            last_period = trailing["quarters"][0].get("period")
            if last_period:
                try:
                    last_report = datetime.fromisoformat(last_period).replace(tzinfo=timezone.utc)
                except ValueError:
                    last_report = None

        result: dict[str, Any] = {}
        if latest_past or trailing:
            result["last_report_date"] = last_report.date().isoformat() if last_report else None
            result["days_since_last_report"] = (
                (asof.date() - last_report.date()).days if last_report else None
            )
            if latest_past:
                if latest_past.eps_actual is not None:
                    result["last_actual"] = latest_past.eps_actual
                if latest_past.eps_estimate is not None:
                    result["last_estimate"] = latest_past.eps_estimate
                if latest_past.eps_actual is not None and latest_past.eps_estimate not in (None, 0):
                    result["last_surprise_pct"] = round(
                        (latest_past.eps_actual - latest_past.eps_estimate) / abs(latest_past.eps_estimate) * 100,
                        2,
                    )
                result["last_report_hour"] = latest_past.report_hour
            elif trailing:
                result["last_actual"] = trailing.get("last_actual")
                result["last_estimate"] = trailing.get("last_estimate")
                result["last_surprise_pct"] = trailing.get("last_surprise_pct")
            if trailing:
                result["quarters"] = trailing.get("quarters", [])

        if next_upcoming:
            result["next_report_date"] = next_upcoming.report_date.date().isoformat()
            result["days_to_next_report"] = (next_upcoming.report_date.date() - asof.date()).days
            result["next_report_hour"] = next_upcoming.report_hour

        return result or None

    @staticmethod
    def _is_regular_session_bar(ts: datetime) -> bool:
        ts = ensure_utc(ts)
        local = ts.astimezone(AnalysisService._NY_TZ)
        local_clock = local.timetz().replace(tzinfo=None)
        return local.weekday() < 5 and (local_clock.hour, local_clock.minute) >= (9, 30) and (local_clock.hour, local_clock.minute) < (16, 0)

    def _regular_bar_at_or_after(self, session: Session, ticker: str, ts: datetime) -> Bar1m | None:
        bars = (
            session.execute(
                select(Bar1m)
                .where(and_(Bar1m.ticker == ticker, Bar1m.ts >= ensure_utc(ts), Bar1m.ts < ensure_utc(ts) + timedelta(days=5)))
                .order_by(Bar1m.ts.asc())
            )
            .scalars()
            .all()
        )
        for bar in bars:
            if self._is_regular_session_bar(bar.ts):
                return bar
        return None

    def _earnings_release_anchor(self, report_date: datetime, report_hour: str | None) -> datetime:
        base = ensure_utc(report_date).replace(hour=0, minute=0, second=0, microsecond=0)
        hour = (report_hour or "").strip().lower()
        if hour in {"amc", "after market close"} or "after market" in hour:
            return base + timedelta(hours=21)
        if hour in {"bmo", "before market open"} or "before market" in hour:
            return base + timedelta(hours=13)
        return base

    def _reaction_2h_after_anchor(self, session: Session, ticker: str, anchor: datetime) -> float | None:
        entry_bar = self._regular_bar_at_or_after(session, ticker, anchor)
        exit_bar = (
            self._regular_bar_at_or_after(session, ticker, ensure_utc(entry_bar.ts) + timedelta(minutes=120))
            if entry_bar
            else None
        )
        if not entry_bar or not exit_bar or abs(float(entry_bar.open)) < 1e-9:
            return None
        return ((float(exit_bar.close) - float(entry_bar.open)) / float(entry_bar.open)) * 100.0

    def _label_earnings_signal(self, event_type: str | None, text: str, surprise_pct: float | None = None) -> str:
        lowered = (text or "").lower()
        if surprise_pct is not None:
            if surprise_pct >= 2.0:
                return "beat"
            if surprise_pct <= -2.0:
                return "miss"
        if event_type in {"earnings_miss", "guidance_cut"} or self._EARNINGS_NEGATIVE_RE.search(lowered):
            return "miss"
        if self._EARNINGS_POSITIVE_RE.search(lowered):
            return "beat"
        return "inline"

    def build_earnings_review(
        self,
        session: Session | None,
        ticker: str,
        event_ts: datetime,
        history_limit: int = 8,
    ) -> dict[str, Any] | None:
        if session is None:
            return None

        ticker = str(ticker or "").upper().strip()
        if not ticker:
            return None

        asof = ensure_utc(event_ts)
        cache_key = ("earnings_review", ticker, asof.date().isoformat(), history_limit)
        cached = self._earnings_review_cache.get(cache_key)
        if cached:
            return dict(cached)

        rows = (
            session.execute(
                select(EarningsCalendar)
                .where(EarningsCalendar.symbol == ticker, EarningsCalendar.report_date <= asof)
                .order_by(EarningsCalendar.report_date.desc())
                .limit(history_limit)
            )
            .scalars()
            .all()
        )

        review_rows: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        beat_count = 0
        miss_count = 0
        beat_and_drop = 0
        miss_and_pop = 0
        beat_returns: list[float] = []
        miss_returns: list[float] = []
        all_returns: list[float] = []

        for row in rows:
            surprise_pct = None
            if row.eps_actual is not None and row.eps_estimate not in (None, 0):
                surprise_pct = ((row.eps_actual - row.eps_estimate) / abs(row.eps_estimate)) * 100.0

            anchor = self._earnings_release_anchor(row.report_date, row.report_hour)
            reaction_2h_pct = self._reaction_2h_after_anchor(session, ticker, anchor)
            if reaction_2h_pct is not None:
                all_returns.append(reaction_2h_pct)

            label = self._label_earnings_signal("earnings_miss", "", surprise_pct=surprise_pct)
            if label == "beat":
                beat_count += 1
                if reaction_2h_pct is not None:
                    beat_returns.append(reaction_2h_pct)
                    if reaction_2h_pct < 0:
                        beat_and_drop += 1
            elif label == "miss":
                miss_count += 1
                if reaction_2h_pct is not None:
                    miss_returns.append(reaction_2h_pct)
                    if reaction_2h_pct > 0:
                        miss_and_pop += 1

            report_date_key = ensure_utc(row.report_date).date().isoformat()
            seen_dates.add(report_date_key)

            review_rows.append(
                {
                    "report_date": report_date_key,
                    "report_hour": row.report_hour,
                    "surprise_pct": round(surprise_pct, 2) if surprise_pct is not None else None,
                    "label": label,
                    "reaction_2h_pct": round(reaction_2h_pct, 2) if reaction_2h_pct is not None else None,
                    "source": "earnings_calendar",
                }
            )

        if len(review_rows) < history_limit:
            recent_events = (
                session.execute(
                    select(Event).where(Event.event_time <= asof).order_by(Event.event_time.desc()).limit(300)
                )
                .scalars()
                .all()
            )
            for event in recent_events:
                if len(review_rows) >= history_limit:
                    break
                if ticker not in [str(t).upper() for t in (event.tickers or [])]:
                    continue
                summary = event.summary or ""
                lowered = summary.lower()
                if event.event_type not in {"earnings_miss", "guidance_cut", "sec_earnings_release"} and "earnings" not in lowered and "guidance" not in lowered:
                    continue
                report_date_key = ensure_utc(event.event_time).date().isoformat()
                if report_date_key in seen_dates:
                    continue

                reaction_2h_pct = self._reaction_2h_after_anchor(session, ticker, ensure_utc(event.event_time))
                if reaction_2h_pct is not None:
                    all_returns.append(reaction_2h_pct)
                label = self._label_earnings_signal(event.event_type, summary)
                if label == "beat":
                    beat_count += 1
                    if reaction_2h_pct is not None:
                        beat_returns.append(reaction_2h_pct)
                        if reaction_2h_pct < 0:
                            beat_and_drop += 1
                elif label == "miss":
                    miss_count += 1
                    if reaction_2h_pct is not None:
                        miss_returns.append(reaction_2h_pct)
                        if reaction_2h_pct > 0:
                            miss_and_pop += 1

                review_rows.append(
                    {
                        "report_date": report_date_key,
                        "report_hour": None,
                        "surprise_pct": None,
                        "label": label,
                        "reaction_2h_pct": round(reaction_2h_pct, 2) if reaction_2h_pct is not None else None,
                        "source": "event_fallback",
                    }
                )
                seen_dates.add(report_date_key)

        if not review_rows:
            return None

        beat_and_drop_rate = (beat_and_drop / beat_count) if beat_count else None
        miss_and_pop_rate = (miss_and_pop / miss_count) if miss_count else None
        avg_beat_reaction = (sum(beat_returns) / len(beat_returns)) if beat_returns else None
        avg_miss_reaction = (sum(miss_returns) / len(miss_returns)) if miss_returns else None
        avg_all_reaction = (sum(all_returns) / len(all_returns)) if all_returns else None

        high_bar_score = 0.0
        if beat_and_drop_rate is not None:
            high_bar_score += beat_and_drop_rate * 70.0
        if avg_beat_reaction is not None and avg_beat_reaction < 0:
            high_bar_score += min(20.0, abs(avg_beat_reaction) * 4.0)
        if miss_and_pop_rate is not None:
            high_bar_score += miss_and_pop_rate * 10.0
        if len(review_rows) < 3:
            high_bar_score = min(high_bar_score, 55.0)
        high_bar_score = round(max(0.0, min(high_bar_score, 100.0)), 1)

        verdict = "GOOD"
        rationale = "earnings profile looks normal"
        if beat_count + miss_count == 0 and len(review_rows) >= 2:
            verdict = "MARGINAL"
            rationale = "earnings reaction history exists, but beat/miss labeling is still ambiguous; expectation gap is unresolved"
        elif high_bar_score >= 80:
            verdict = "POOR"
            rationale = "historically this ticker often sells off even after positive earnings surprises"
        elif high_bar_score >= 60:
            verdict = "MARGINAL"
            rationale = "this ticker has a high-bar earnings profile; simple beats are often not enough"

        allow_upcoming_schedule = asof >= utc_now() - timedelta(days=2)
        context = self._earnings_calendar_context(
            session,
            ticker,
            asof,
            include_upcoming=allow_upcoming_schedule,
        ) or {}
        if context.get("days_to_next_report") is not None and context["days_to_next_report"] <= 3:
            verdict = "MARGINAL" if verdict == "GOOD" else verdict
            rationale = "next earnings report is very close; event trades face elevated gap risk"

        result = {
            "ticker": ticker,
            "as_of_utc": asof.isoformat(),
            "sample_size": len(review_rows),
            "history": review_rows,
            "beat_and_drop_rate": round(beat_and_drop_rate, 3) if beat_and_drop_rate is not None else None,
            "miss_and_pop_rate": round(miss_and_pop_rate, 3) if miss_and_pop_rate is not None else None,
            "avg_2h_reaction_on_beats_pct": round(avg_beat_reaction, 2) if avg_beat_reaction is not None else None,
            "avg_2h_reaction_on_misses_pct": round(avg_miss_reaction, 2) if avg_miss_reaction is not None else None,
            "avg_2h_reaction_all_pct": round(avg_all_reaction, 2) if avg_all_reaction is not None else None,
            "high_bar_score": high_bar_score,
            "tradeability": verdict,
            "reason": rationale,
        }
        self._earnings_review_cache[cache_key] = dict(result)
        return result

    def _finnhub_daily_candles(
        self,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> list[dict[str, Any]] | None:
        data = self._fh_get(
            "/stock/candle",
            {
                "symbol": symbol,
                "resolution": "D",
                "from": int(ensure_utc(start_ts).timestamp()),
                "to": int(ensure_utc(end_ts).timestamp()),
            },
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None
        closes = data.get("c") or []
        highs = data.get("h") or []
        lows = data.get("l") or []
        opens = data.get("o") or []
        times = data.get("t") or []
        candles: list[dict[str, Any]] = []
        for idx, ts in enumerate(times):
            try:
                candles.append(
                    {
                        "ts": datetime.fromtimestamp(int(ts), tz=timezone.utc),
                        "open": float(opens[idx]),
                        "high": float(highs[idx]),
                        "low": float(lows[idx]),
                        "close": float(closes[idx]),
                    }
                )
            except Exception:
                continue
        return candles or None

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

    def _macro_market_context(self, event_ts: datetime) -> dict | None:
        lookback_days = max(7, int(getattr(self.settings, "macro_context_lookback_days", 30)))
        end_ts = ensure_utc(event_ts)
        start_ts = end_ts - timedelta(days=lookback_days + 7)
        symbols = ["SPY", "QQQ", "IWM", "TLT", "XLK", "XLF", "XLE", "XLV", "XLI"]
        returns_pct: dict[str, float] = {}

        for symbol in symbols:
            candles = self._finnhub_daily_candles(symbol, start_ts=start_ts, end_ts=end_ts)
            if not candles or len(candles) < 2:
                continue
            start_close = candles[0]["close"]
            end_close = candles[-1]["close"]
            if abs(start_close) < 1e-9:
                continue
            returns_pct[symbol] = round((end_close - start_close) / start_close * 100, 2)

        if "SPY" not in returns_pct:
            return None

        sector_returns = {symbol: value for symbol, value in returns_pct.items() if symbol.startswith("XL")}
        leader = max(sector_returns.items(), key=lambda item: item[1]) if sector_returns else None
        laggard = min(sector_returns.items(), key=lambda item: item[1]) if sector_returns else None
        breadth_positive = sum(1 for symbol in ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI") if returns_pct.get(symbol, 0.0) > 0)

        spy_ret = returns_pct.get("SPY", 0.0)
        qqq_ret = returns_pct.get("QQQ", 0.0)
        iwm_ret = returns_pct.get("IWM", 0.0)
        tlt_ret = returns_pct.get("TLT", 0.0)
        if spy_ret >= 3.0 and qqq_ret >= spy_ret and breadth_positive >= 5:
            regime = "RISK_ON"
            narrative = "broad risk-on tape over the last month, led by growth and supported by decent breadth"
        elif spy_ret <= -3.0 and tlt_ret > spy_ret:
            regime = "RISK_OFF"
            narrative = "risk-off tape over the last month, with defensives or duration outperforming equities"
        elif leader and leader[0] == "XLE" and leader[1] >= spy_ret + 3.0:
            regime = "SECTOR_ROTATION"
            narrative = "rotation-driven market where energy leadership is stronger than the broad tape"
        else:
            regime = "MIXED"
            narrative = "mixed market over the last month with no clean broad-market trend"

        return {
            "lookback_days": lookback_days,
            "as_of_utc": end_ts.isoformat(),
            "index_returns_pct": {
                symbol: returns_pct[symbol]
                for symbol in ("SPY", "QQQ", "IWM", "TLT")
                if symbol in returns_pct
            },
            "sector_returns_pct": sector_returns,
            "breadth_positive_count": breadth_positive,
            "leadership": {"symbol": leader[0], "return_pct": leader[1]} if leader else None,
            "laggard": {"symbol": laggard[0], "return_pct": laggard[1]} if laggard else None,
            "regime": regime,
            "narrative": narrative,
        }

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
        allow_upcoming_schedule = event_ts >= utc_now() - timedelta(days=2)
        earnings_ctx = self._earnings_calendar_context(
            session,
            ticker,
            event_ts,
            include_upcoming=allow_upcoming_schedule,
        )
        earnings_review = self.build_earnings_review(session, ticker, event_ts)
        is_recent_event = event_ts >= utc_now() - timedelta(days=2)
        tech_signal = self._finnhub_tech_signal(ticker) if is_recent_event else None
        sr_levels = self._finnhub_support_resistance(ticker, current_price) if is_recent_event else None
        analyst_consensus = self._finnhub_analyst_consensus(ticker) if is_recent_event else None
        macro_context = self._macro_market_context(event_ts)

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
        if earnings_review:
            features["earnings_review"] = earnings_review
        if tech_signal:
            features["tech_signal"] = tech_signal
        if sr_levels:
            features["support_resistance"] = sr_levels
        if analyst_consensus:
            features["analyst_consensus"] = analyst_consensus
        if macro_context:
            features["macro_market_context"] = macro_context
            features["macro_market_regime"] = {
                "regime": macro_context.get("regime"),
                "lookback_days": macro_context.get("lookback_days"),
                "spy_lookback_return_pct": (macro_context.get("index_returns_pct") or {}).get("SPY"),
                "narrative": macro_context.get("narrative"),
            }

        return features

    def _analysis_event_type(self, event: Event, session: Session | None = None) -> str:
        text_parts = [event.summary or ""]
        if session is not None:
            for row in self._evidence_rows(session, event, limit=3):
                title = str(row.get("title") or "")
                full_text = str(row.get("full_text") or "")
                if title:
                    text_parts.append(title)
                if full_text:
                    text_parts.append(full_text[:1200])
        return resolve_event_type_for_text(event.event_type, "\n".join(part for part in text_parts if part))

    def _llm_extract(self, event: Event, session: Session | None = None) -> dict:
        analysis_event_type = self._analysis_event_type(event, session=session)
        prior_direction = self._event_prior_direction(analysis_event_type)
        evidence_payload = self._build_evidence_payload(session, event)
        market_features = self._event_market_features(session, event)
        prompt = {
            "task": "Classify likely near-term direction impact for the mentioned US equity event.",
            "event": {
                "event_id": event.id,
                "event_time": event.event_time.isoformat() if event.event_time else None,
                "event_type": analysis_event_type,
                "original_event_type": event.event_type,
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
                "EVENT-TYPE SANITY CHECK: If the stored event_type conflicts with the article text, trust the article text. Examples: 'guides above estimates', 'higher sales', 'wins case', 'complete victory', or 'settles litigation' should not be treated as automatically negative just because the stored label is negative.",
                "MERGER/ACQUISITION RULE: If event_type is 'merger_acquisition' and the ticker is the TARGET (being acquired), direction is almost always UP (acquisition premium). Only choose DOWN if the ticker is the ACQUIRER paying a very high premium with clear negative market reaction evidence.",
                "Output JSON only.",
                "direction must be exactly one of: UP, DOWN, NEUTRAL.",
                "Do not output trading actions (BUY/SELL/SHORT/HOLD).",
                "If direction is UP or DOWN, output position_pct in [0,1] as the fraction of max allowed position to use.",
                "Position sizing bands: 0.75-1.00 only for hard, ticker-specific, high-confidence catalysts with direct evidence; 0.45-0.70 for clear but less exceptional catalysts; 0.10-0.35 for weaker but still tradeable setups; 0 for non-tradeable or ambiguous content.",
                "If the article is valuation/opinion/technical commentary, price-action recap, or broad market roundup, output NEUTRAL and position_pct=0.",
                "THIS IS A SHORT-TERM SIGNAL (next 1-4 hours). Judge the IMMEDIATE price reaction to the news event, NOT the long-term fundamental outlook. A company may be bullish long-term but still drop short-term on bad news.",
                "Primary signal: news content and event severity. Ask: will this news cause buyers or sellers to act in the next 1-4 hours?",
                "Use NEUTRAL only when the news is truly routine (e.g. minor analyst note, no surprise) or evidence is contradictory.",
                "Secondary signals (use only as tie-breakers when news signal is ambiguous):",
                "  - tech_signal: 'buy' supports UP, 'sell' supports DOWN",
                "  - relative_strength_vs_spy_pct: if ticker is already outperforming SPY today, UP news has more momentum",
                "  - earnings_context: use only the earnings data already available as of event_time; positive surprise_pct supports UP, negative supports DOWN; upcoming earnings within a few days raises gap risk and can justify smaller size",
                "  - earnings_review: this is the ticker's historical earnings reaction profile. If high_bar_score is high or beat_and_drop_rate is elevated, do NOT assume a simple beat is bullish. Prefer HOLD or smaller size unless the current report is clearly exceptional and price confirms.",
                "  - support_resistance: price within 1% of resistance reduces upside; within 1% of support reduces downside",
                "  - analyst_consensus: BULLISH (consensus_score>0.2) is a mild UP tailwind; BEARISH is a mild DOWN tailwind — but NEVER override a strong news signal",
                "  - macro_market_context: this is a last-month market narrative, including index/sector returns and regime. Use it as a DIRECTIONAL TILT only when the company-specific news is ambiguous. NEVER override a clear, ticker-specific hard catalyst based on macro alone.",
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
        analysis_event_type = resolve_event_type_for_text(event.event_type, event.summary or "")
        return {
            "action": fallback_action(analysis_event_type),
            "ticker": event.tickers[0] if event.tickers else "",
            "horizon_min": self.settings.default_horizon_min,
            "horizon_profile": None,
            "position_pct_suggestion": None,
            "reason": f"fallback rule for {analysis_event_type}",
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

    def event_to_signal(
        self,
        event: Event,
        session: Session | None = None,
        use_tradeability_filter: bool | None = None,
    ) -> TradeSignal | None:
        if not event.tickers:
            return None

        if use_tradeability_filter is None:
            use_tradeability_filter = self.settings.event_tradeability_filter_enabled
        if use_tradeability_filter:
            tradeability = self.assess_tradeability(event, session=session)
            if not tradeability.get("tradeable", True):
                return TradeSignal(
                    action="HOLD",
                    ticker=event.tickers[0],
                    confidence=event.confidence,
                    horizon_min=self.settings.default_horizon_min,
                    horizon_profile=None,
                    position_pct_suggestion=0.0,
                    reason=f"tradeability_filtered:{tradeability.get('reason', 'low_quality')}",
                    expires_at=utc_now() + timedelta(minutes=self.settings.default_horizon_min),
                    fallback_used=False,
                )

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
