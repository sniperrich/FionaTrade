"""Signal Validation Layer — sits AFTER primary analysis, BEFORE execution.

This is a reviewer / risk gate, NOT a second directional predictor.
It evaluates execution quality along several orthogonal dimensions and outputs
a structured recommendation that the execution layer consumes.

Pipeline position:
    Event  →  AnalysisService.event_to_signal()  →  SignalValidator.validate()  →  execute / skip

Key design choices:
- Deterministic rule-based checks only (no second LLM call, no external I/O).
- All inputs are already in-memory; validation is cheap and synchronous.
- Future hooks are reserved: market_context, secondary_llm_review, narrative_tracker.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from app.analysis.taxonomy import (
    EXCLUDED_FROM_TRADING,
    NEGATIVE_EVENTS,
    POSITIVE_EVENTS,
)
from app.db.models import Event
from app.schemas.types import TradeSignal

logger = logging.getLogger(__name__)

# ── Enums ─────────────────────────────────────────────────────────────────────


class Novelty(str, Enum):
    NEW = "NEW"
    PARTIALLY_KNOWN = "PARTIALLY_KNOWN"
    STALE = "STALE"
    DUPLICATE = "DUPLICATE"


class EventStrength(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NOISE = "NOISE"


class PricedInRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Tradeability(str, Enum):
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    POOR = "POOR"


class Consistency(str, Enum):
    STRONG = "STRONG"
    MIXED = "MIXED"
    WEAK = "WEAK"


class ExecutionRecommendation(str, Enum):
    APPROVE = "APPROVE"
    DOWNWEIGHT = "DOWNWEIGHT"
    REJECT = "REJECT"
    NO_TRADE = "NO_TRADE"


# ── Result Model ──────────────────────────────────────────────────────────────


@dataclass
class SignalValidationResult:
    """Structured output of the Signal Validation Layer.

    review_score (0–100): execution quality score, independent of primary confidence.
    execution_recommendation: APPROVE / DOWNWEIGHT / REJECT / NO_TRADE.
    issue_tags: machine-readable list of detected problems.
    rationale: human-readable list of reasoning sentences.

    Note: review_score is about execution quality, NOT directional confidence.
    """

    novelty: Novelty
    event_strength: EventStrength
    priced_in_risk: PricedInRisk
    tradeability: Tradeability
    consistency: Consistency
    issue_tags: list[str] = field(default_factory=list)
    review_score: int = 0           # 0–100
    execution_recommendation: ExecutionRecommendation = ExecutionRecommendation.NO_TRADE
    rationale: list[str] = field(default_factory=list)


# ── Optional context containers ───────────────────────────────────────────────


@dataclass
class PriceContext:
    """Snapshot of price behaviour around the event.

    All fields are optional — validator degrades gracefully when absent.
    """

    current_price: float | None = None          # price at/near signal generation
    event_price: float | None = None            # price at moment of event
    intraday_return_pct: float | None = None    # ticker intraday return so far
    spy_return_pct: float | None = None         # SPY intraday return (market beta)
    pct_to_resistance: float | None = None      # % from current price to nearest resistance
    pct_from_support: float | None = None       # % current price is above nearest support


@dataclass
class MarketContext:
    """Placeholder for future market-regime context.

    Intentionally lightweight — do not implement regime detection now.
    Callers may pass None; validator ignores it gracefully.
    """

    regime: str | None = None           # e.g. "BULL", "BEAR", "CHOPPY"
    vix_level: float | None = None      # spot VIX
    notes: str | None = None


# ── Noise-headline patterns (reuse across callers) ────────────────────────────

_NOISE_TITLE_RE = re.compile(
    r"\b(trending tickers|market movers|early movers|morning movers"
    r"|benzinga market summary|top stocks|stocks to watch"
    r"|should you invest|is .{3,40} (a )?(good|bad) (buy|investment)"
    r"|notable calls|analyst upgrade|analyst downgrade"
    r"|stock market today|wall street lunch|market wrap"
    r"|government shutdown|equity (indexes|futures)|ftse \d+"
    r"|jobs report|bitcoin price|crypto price)\b",
    re.IGNORECASE,
)

_STRONG_EVENT_TYPES = frozenset(
    {"financial_fraud", "audit_issue", "regulatory_penalty", "accident_disaster"}
)
_MODERATE_EVENT_TYPES = frozenset(
    {"earnings_miss", "guidance_cut", "major_litigation", "supply_chain_disruption"}
)


# ── Validator ─────────────────────────────────────────────────────────────────


class SignalValidator:
    """Stateless rule-based reviewer that gates execution quality.

    Usage::

        validator = SignalValidator(settings)
        result = validator.validate(
            event=event,
            signal=signal,
            price_context=price_ctx,  # optional
            recent_event_summaries=[],  # optional: titles of recent events for same ticker
            market_context=None,  # reserved for future use
        )
        if result.execution_recommendation in (APPROVE, DOWNWEIGHT):
            execute(signal)

    The validator never modifies its inputs.
    """

    def __init__(self, settings: Any) -> None:
        # Accept any settings-like object; access only known attributes.
        self._stale_minutes: int = getattr(settings, "validation_stale_minutes", 120)
        self._price_move_threshold_pct: float = getattr(
            settings, "validation_price_move_threshold_pct", 3.0
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def validate(
        self,
        event: Event,
        signal: TradeSignal,
        price_context: PriceContext | None = None,
        recent_event_summaries: list[str] | None = None,
        market_context: MarketContext | None = None,  # reserved, currently unused
        reference_time: datetime | None = None,
    ) -> SignalValidationResult:
        """Run all validation checks and return a structured result.

        Args:
            reference_time: Override "now" for staleness checks. Pass event.event_time
                in backtests so historical events are not falsely rejected as stale.
        """
        now = reference_time if reference_time is not None else datetime.now(tz=timezone.utc)
        issue_tags: list[str] = []
        rationale: list[str] = []

        # ── Dimension checks ─────────────────────────────────────────────────

        novelty = self._check_novelty(event, recent_event_summaries, now, issue_tags, rationale)
        event_strength = self._check_event_strength(event, signal, issue_tags, rationale)
        priced_in_risk = self._check_priced_in_risk(event, signal, price_context, now, issue_tags, rationale)
        tradeability = self._check_tradeability(event, signal, price_context, issue_tags, rationale)
        consistency = self._check_consistency(event, signal, issue_tags, rationale)

        # ── Score computation ─────────────────────────────────────────────────

        review_score = self._compute_score(
            novelty, event_strength, priced_in_risk, tradeability, consistency, issue_tags
        )

        # ── Final recommendation ──────────────────────────────────────────────

        recommendation = self._recommend(
            novelty, event_strength, priced_in_risk, tradeability, consistency,
            review_score, issue_tags,
        )

        result = SignalValidationResult(
            novelty=novelty,
            event_strength=event_strength,
            priced_in_risk=priced_in_risk,
            tradeability=tradeability,
            consistency=consistency,
            issue_tags=issue_tags,
            review_score=review_score,
            execution_recommendation=recommendation,
            rationale=rationale,
        )

        self._log(event, signal, result)
        return result

    # ── Dimension: Novelty ────────────────────────────────────────────────────

    def _check_novelty(
        self,
        event: Event,
        recent_event_summaries: list[str] | None,
        now: datetime,
        issue_tags: list[str],
        rationale: list[str],
    ) -> Novelty:
        event_time = self._ensure_utc(event.event_time)
        age_minutes = (now - event_time).total_seconds() / 60.0

        # Staleness: event is too old to generate a fresh 1–4h reaction.
        if age_minutes > self._stale_minutes:
            issue_tags.append("stale_news")
            rationale.append(
                f"Event is {age_minutes:.0f} min old (threshold {self._stale_minutes} min); "
                "market likely already absorbed this."
            )
            return Novelty.STALE

        # Duplicate detection: fuzzy match against recent summaries for same ticker.
        if recent_event_summaries:
            summary = (event.summary or "").lower()
            for other in recent_event_summaries:
                if other and self._summaries_overlap(summary, other.lower()):
                    issue_tags.append("duplicate_event")
                    rationale.append("Summary closely matches a recently seen event for this ticker.")
                    return Novelty.DUPLICATE

        # Partially known: event is fresh but event_time is early in the day
        # and the market has been open for >2h (information partially absorbed).
        if age_minutes > 30:
            rationale.append(
                f"Event is {age_minutes:.0f} min old; partially absorbed but still fresh enough."
            )
            return Novelty.PARTIALLY_KNOWN

        rationale.append(f"Event is {age_minutes:.0f} min old — fresh signal.")
        return Novelty.NEW

    # ── Dimension: Event Strength ─────────────────────────────────────────────

    def _check_event_strength(
        self,
        event: Event,
        signal: TradeSignal,
        issue_tags: list[str],
        rationale: list[str],
    ) -> EventStrength:
        event_type = event.event_type or ""
        severity = event.severity or 0
        summary = event.summary or ""
        confidence = event.confidence or 0

        # Noise headline pattern
        if _NOISE_TITLE_RE.search(summary):
            issue_tags.append("noise_headline")
            rationale.append("Event summary matches a generic market-round-up headline pattern.")
            return EventStrength.NOISE

        # Unknown / excluded event type — no directional edge
        if event_type in EXCLUDED_FROM_TRADING:
            issue_tags.append("excluded_event_type")
            rationale.append(f"Event type '{event_type}' is excluded from trading (no directional edge).")
            return EventStrength.NOISE

        # Ticker mismatch: event tickers don't include signal ticker
        signal_ticker = (signal.ticker or "").upper()
        event_tickers = [str(t).upper() for t in (event.tickers or [])]
        if signal_ticker and event_tickers and signal_ticker not in event_tickers:
            issue_tags.append("ticker_mismatch")
            rationale.append(
                f"Signal ticker {signal_ticker} not in event tickers {event_tickers}; "
                "event may be mis-tagged."
            )

        # Strength tiers
        if event_type in _STRONG_EVENT_TYPES or severity >= 85:
            rationale.append(f"Strong event type '{event_type}' with severity={severity}.")
            return EventStrength.STRONG

        if event_type in _MODERATE_EVENT_TYPES or severity >= 70:
            rationale.append(f"Moderate event type '{event_type}' with severity={severity}.")
            return EventStrength.MODERATE

        if event_type in POSITIVE_EVENTS or event_type in NEGATIVE_EVENTS:
            if confidence >= 60:
                rationale.append(f"Weak-but-classified event type '{event_type}', confidence={confidence}.")
                return EventStrength.WEAK
            issue_tags.append("low_confidence_event")
            rationale.append(f"Event confidence={confidence} is below threshold for '{event_type}'.")
            return EventStrength.NOISE

        issue_tags.append("unclassified_event_type")
        rationale.append(f"Event type '{event_type}' has no known directional bias.")
        return EventStrength.WEAK

    # ── Dimension: Priced-In Risk ─────────────────────────────────────────────

    def _check_priced_in_risk(
        self,
        event: Event,
        signal: TradeSignal,
        price_context: PriceContext | None,
        now: datetime,
        issue_tags: list[str],
        rationale: list[str],
    ) -> PricedInRisk:
        if price_context is None:
            # No price data — can't assess; treat as low risk rather than blocking.
            rationale.append("No price context available; priced-in risk assumed LOW.")
            return PricedInRisk.LOW

        intraday = price_context.intraday_return_pct
        spy = price_context.spy_return_pct
        threshold = self._price_move_threshold_pct

        # Large move already in the direction of the signal → likely priced in.
        if intraday is not None:
            intraday_pct = intraday * 100.0
            relative = (intraday - (spy or 0.0)) * 100.0

            if signal.action == "BUY" and intraday_pct > threshold:
                issue_tags.append("large_move_before_entry")
                rationale.append(
                    f"Ticker already moved +{intraday_pct:.1f}% today (>{threshold}%); "
                    "UP move may be priced in."
                )
                return PricedInRisk.HIGH

            if signal.action in ("SHORT", "SELL") and intraday_pct < -threshold:
                issue_tags.append("large_move_before_entry")
                rationale.append(
                    f"Ticker already dropped {intraday_pct:.1f}% today (>-{threshold}%); "
                    "DOWN move may be priced in."
                )
                return PricedInRisk.HIGH

            # Moderate move: flag as medium risk
            if abs(relative) > threshold / 2.0:
                rationale.append(
                    f"Ticker has relative move of {relative:+.1f}% vs SPY — moderate priced-in risk."
                )
                return PricedInRisk.MEDIUM

        # Near resistance on a BUY signal
        pct_to_res = price_context.pct_to_resistance
        if signal.action == "BUY" and pct_to_res is not None and 0 < pct_to_res < 1.0:
            issue_tags.append("near_resistance")
            rationale.append(
                f"Price within {pct_to_res:.1f}% of resistance; limited upside room for BUY."
            )
            return PricedInRisk.MEDIUM

        # Near support on a SHORT signal
        pct_from_sup = price_context.pct_from_support
        if signal.action in ("SHORT", "SELL") and pct_from_sup is not None and 0 < pct_from_sup < 1.0:
            issue_tags.append("near_support")
            rationale.append(
                f"Price within {pct_from_sup:.1f}% of support; limited downside room for SHORT."
            )
            return PricedInRisk.MEDIUM

        rationale.append("Price context looks clean; priced-in risk LOW.")
        return PricedInRisk.LOW

    # ── Dimension: Tradeability ───────────────────────────────────────────────

    def _check_tradeability(
        self,
        event: Event,
        signal: TradeSignal,
        price_context: PriceContext | None,
        issue_tags: list[str],
        rationale: list[str],
    ) -> Tradeability:
        tickers = event.tickers or []

        # No ticker → untradeable
        if not tickers:
            issue_tags.append("no_ticker")
            rationale.append("Event has no tradeable ticker.")
            return Tradeability.POOR

        # Horizon sanity: very short horizon on a low-confidence signal is marginal
        horizon = signal.horizon_min or 0
        confidence = event.confidence or 0
        if horizon < 15:
            issue_tags.append("horizon_too_short")
            rationale.append(f"Horizon {horizon} min is extremely short; execution risk is high.")
            return Tradeability.POOR

        if horizon < 30 and confidence < 60:
            issue_tags.append("short_horizon_low_confidence")
            rationale.append(
                f"Horizon {horizon} min combined with confidence={confidence} makes entry marginal."
            )
            return Tradeability.MARGINAL

        # Price context: if current price is unknown but we have intraday context, still tradeable
        if price_context is not None and price_context.current_price is None:
            if price_context.intraday_return_pct is None:
                issue_tags.append("no_price_data")
                rationale.append("Could not determine current price or intraday return; execution is marginal.")
                return Tradeability.MARGINAL

        rationale.append(f"Ticker(s) {tickers}, horizon={horizon} min — tradeable.")
        return Tradeability.GOOD

    # ── Dimension: Consistency ────────────────────────────────────────────────

    def _check_consistency(
        self,
        event: Event,
        signal: TradeSignal,
        issue_tags: list[str],
        rationale: list[str],
    ) -> Consistency:
        event_type = event.event_type or ""

        # Fallback signal direction vs taxonomy expectation
        if signal.fallback_used:
            issue_tags.append("fallback_signal")
            rationale.append("Signal was generated by rule fallback (LLM unavailable or failed).")

        # Direction vs event-type taxonomy: flag obvious contradiction
        expected_action: str | None = None
        if event_type in POSITIVE_EVENTS:
            expected_action = "BUY"
        elif event_type in NEGATIVE_EVENTS:
            expected_action = "SHORT"

        if expected_action and signal.action not in ("HOLD", expected_action):
            issue_tags.append("direction_vs_taxonomy_conflict")
            rationale.append(
                f"Signal action={signal.action} conflicts with taxonomy expectation "
                f"for '{event_type}' ({expected_action}). LLM may have over-ridden taxonomy."
            )
            return Consistency.MIXED

        if signal.fallback_used:
            return Consistency.MIXED

        rationale.append(f"Signal direction={signal.action} is consistent with '{event_type}' taxonomy.")
        return Consistency.STRONG

    # ── Score Computation ─────────────────────────────────────────────────────

    def _compute_score(
        self,
        novelty: Novelty,
        event_strength: EventStrength,
        priced_in_risk: PricedInRisk,
        tradeability: Tradeability,
        consistency: Consistency,
        issue_tags: list[str],
    ) -> int:
        """Compute review_score (0–100) from dimension grades.

        Weights are intentionally simple and auditable.
        review_score reflects execution quality, not directional edge.
        """
        # Novelty (max 25)
        novelty_pts = {
            Novelty.NEW: 25,
            Novelty.PARTIALLY_KNOWN: 15,
            Novelty.STALE: 0,
            Novelty.DUPLICATE: 0,
        }[novelty]

        # Event strength (max 30)
        strength_pts = {
            EventStrength.STRONG: 30,
            EventStrength.MODERATE: 22,
            EventStrength.WEAK: 10,
            EventStrength.NOISE: 0,
        }[event_strength]

        # Priced-in risk (max 20)
        priced_pts = {
            PricedInRisk.LOW: 20,
            PricedInRisk.MEDIUM: 10,
            PricedInRisk.HIGH: 0,
        }[priced_in_risk]

        # Tradeability (max 15)
        trade_pts = {
            Tradeability.GOOD: 15,
            Tradeability.MARGINAL: 8,
            Tradeability.POOR: 0,
        }[tradeability]

        # Consistency (max 10)
        consist_pts = {
            Consistency.STRONG: 10,
            Consistency.MIXED: 5,
            Consistency.WEAK: 0,
        }[consistency]

        # Tag penalties: each unique critical tag knocks off points
        _CRITICAL_TAG_PENALTIES: dict[str, int] = {
            "noise_headline": 20,
            "excluded_event_type": 25,
            "ticker_mismatch": 15,
            "large_move_before_entry": 10,
            "near_resistance": 5,
            "near_support": 5,
            "no_ticker": 30,
            "horizon_too_short": 20,
            "direction_vs_taxonomy_conflict": 8,
            "stale_news": 25,
            "duplicate_event": 25,
            "low_confidence_event": 10,
        }
        tag_penalty = sum(_CRITICAL_TAG_PENALTIES.get(t, 0) for t in set(issue_tags))

        raw = novelty_pts + strength_pts + priced_pts + trade_pts + consist_pts - tag_penalty
        return max(0, min(100, raw))

    # ── Recommendation ────────────────────────────────────────────────────────

    def _recommend(
        self,
        novelty: Novelty,
        event_strength: EventStrength,
        priced_in_risk: PricedInRisk,
        tradeability: Tradeability,
        consistency: Consistency,
        review_score: int,
        issue_tags: list[str],
    ) -> ExecutionRecommendation:
        """Translate dimension grades into a single execution recommendation.

        Hard REJECT conditions (any one is sufficient):
        - Stale or duplicate news
        - Noise event or untradeable ticker
        - Review score below minimum threshold

        NO_TRADE conditions:
        - Priced-in risk HIGH with weak/noise strength
        - Tradeability POOR

        DOWNWEIGHT:
        - Score is acceptable but partially known, medium priced-in risk,
          marginal tradeability, or mixed consistency

        APPROVE: everything looks good
        """
        # ── Hard REJECTs ──────────────────────────────────────────────────────
        if novelty in (Novelty.STALE, Novelty.DUPLICATE):
            return ExecutionRecommendation.REJECT

        if event_strength == EventStrength.NOISE:
            return ExecutionRecommendation.REJECT

        if tradeability == Tradeability.POOR:
            return ExecutionRecommendation.NO_TRADE

        if review_score < 20:
            return ExecutionRecommendation.REJECT

        # ── NO_TRADE ──────────────────────────────────────────────────────────
        if priced_in_risk == PricedInRisk.HIGH and event_strength in (EventStrength.WEAK, EventStrength.NOISE):
            return ExecutionRecommendation.NO_TRADE

        # ── DOWNWEIGHT ────────────────────────────────────────────────────────
        downweight_conditions = [
            novelty == Novelty.PARTIALLY_KNOWN,
            priced_in_risk == PricedInRisk.HIGH,
            priced_in_risk == PricedInRisk.MEDIUM and event_strength == EventStrength.WEAK,
            tradeability == Tradeability.MARGINAL,
            consistency == Consistency.MIXED,
            event_strength == EventStrength.WEAK,
            review_score < 50,
            "ticker_mismatch" in issue_tags,
        ]
        if any(downweight_conditions):
            return ExecutionRecommendation.DOWNWEIGHT

        # ── APPROVE ───────────────────────────────────────────────────────────
        return ExecutionRecommendation.APPROVE

    # ── Structured Logging ────────────────────────────────────────────────────

    def _log(self, event: Event, signal: TradeSignal, result: SignalValidationResult) -> None:
        logger.info(
            "signal_validation ticker=%s event_type=%s direction=%s "
            "primary_confidence=%s review_score=%s recommendation=%s "
            "novelty=%s strength=%s priced_in=%s tradeability=%s consistency=%s "
            "tags=%s",
            signal.ticker,
            event.event_type,
            signal.action,
            event.confidence,
            result.review_score,
            result.execution_recommendation.value,
            result.novelty.value,
            result.event_strength.value,
            result.priced_in_risk.value,
            result.tradeability.value,
            result.consistency.value,
            ",".join(result.issue_tags) if result.issue_tags else "none",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_utc(dt: datetime | None) -> datetime:
        if dt is None:
            return datetime.now(tz=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _summaries_overlap(a: str, b: str, min_common_words: int = 5) -> bool:
        """Fuzzy duplicate check: count shared meaningful words."""
        stopwords = {
            "the", "a", "an", "is", "in", "of", "to", "and", "for", "on",
            "at", "as", "by", "with", "its", "it", "be", "has", "have",
        }
        words_a = {w for w in re.findall(r"[a-z]{3,}", a) if w not in stopwords}
        words_b = {w for w in re.findall(r"[a-z]{3,}", b) if w not in stopwords}
        return len(words_a & words_b) >= min_common_words
