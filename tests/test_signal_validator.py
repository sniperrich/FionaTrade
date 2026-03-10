"""Tests for the Signal Validation Layer (app/analysis/signal_validator.py).

These tests verify:
- Strong, fresh events → APPROVE
- Stale events → REJECT
- Duplicate events → REJECT
- Priced-in / overextended move → DOWNWEIGHT or NO_TRADE
- Noise headline → REJECT
- Weak event → DOWNWEIGHT
- Missing optional context does not crash
- Ticker mismatch is flagged but doesn't hard-block
- Direction vs taxonomy conflict → MIXED consistency
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.signal_validator import (
    Consistency,
    EventStrength,
    ExecutionRecommendation,
    MarketContext,
    Novelty,
    PriceContext,
    PricedInRisk,
    SignalValidationResult,
    SignalValidator,
    Tradeability,
)
from app.core.config import Settings
from app.db.models import Event
from app.schemas.types import TradeSignal


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def validator(settings) -> SignalValidator:
    return SignalValidator(settings)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _event(
    event_type: str = "regulatory_penalty",
    tickers: list[str] | None = None,
    severity: int = 85,
    confidence: int = 75,
    summary: str = "SEC charges company with securities fraud",
    event_time: datetime | None = None,
) -> Event:
    e = Event()
    e.id = 1
    e.event_type = event_type
    e.tickers = tickers if tickers is not None else ["AAPL"]
    e.severity = severity
    e.confidence = confidence
    e.summary = summary
    e.event_time = event_time or _now()
    e.validation_status = "VALID"
    return e


def _signal(
    action: str = "SHORT",
    ticker: str = "AAPL",
    confidence: int = 75,
    horizon_min: int = 120,
    fallback_used: bool = False,
    reason: str = "LLM: regulatory penalty warrants short",
) -> TradeSignal:
    return TradeSignal(
        action=action,
        ticker=ticker,
        confidence=confidence,
        horizon_min=horizon_min,
        reason=reason,
        expires_at=_now() + timedelta(minutes=horizon_min),
        fallback_used=fallback_used,
    )


# ── Core happy-path ────────────────────────────────────────────────────────────

def test_strong_fresh_event_approve(validator):
    """Strong, fresh, directionally consistent event should be APPROVED."""
    result = validator.validate(event=_event(), signal=_signal())
    assert result.execution_recommendation == ExecutionRecommendation.APPROVE
    assert result.event_strength == EventStrength.STRONG
    assert result.novelty == Novelty.NEW
    assert result.review_score >= 60


def test_missing_price_context_does_not_crash(validator):
    """Validator must degrade gracefully when price_context is None."""
    result = validator.validate(event=_event(), signal=_signal(), price_context=None)
    assert result.execution_recommendation in ExecutionRecommendation.__members__.values()
    assert 0 <= result.review_score <= 100


def test_missing_market_context_does_not_crash(validator):
    """Validator must degrade gracefully when market_context is None."""
    result = validator.validate(
        event=_event(), signal=_signal(),
        price_context=None, market_context=None,
    )
    assert result is not None


def test_missing_recent_summaries_does_not_crash(validator):
    """Validator must work when recent_event_summaries is None."""
    result = validator.validate(
        event=_event(), signal=_signal(),
        recent_event_summaries=None,
    )
    assert result is not None


# ── Novelty: Stale ────────────────────────────────────────────────────────────

def test_stale_news_rejected(validator):
    """News older than stale_minutes (default 120) should be REJECTED."""
    old_event = _event(event_time=_now() - timedelta(minutes=180))
    result = validator.validate(event=old_event, signal=_signal())
    assert result.novelty == Novelty.STALE
    assert result.execution_recommendation == ExecutionRecommendation.REJECT
    assert "stale_news" in result.issue_tags


def test_slightly_old_news_partially_known(validator):
    """News 45 min old should be PARTIALLY_KNOWN, not STALE."""
    event = _event(event_time=_now() - timedelta(minutes=45))
    result = validator.validate(event=event, signal=_signal())
    assert result.novelty == Novelty.PARTIALLY_KNOWN
    assert result.execution_recommendation != ExecutionRecommendation.REJECT


# ── Novelty: Duplicate ────────────────────────────────────────────────────────

def test_duplicate_event_rejected(validator):
    """Event matching a recent summary closely should be REJECTED as duplicate."""
    event = _event(summary="SEC charges Apple with securities fraud and penalty")
    recent = ["SEC charges apple with securities fraud and penalty enforcement action"]
    result = validator.validate(event=event, signal=_signal(), recent_event_summaries=recent)
    assert result.novelty == Novelty.DUPLICATE
    assert result.execution_recommendation == ExecutionRecommendation.REJECT
    assert "duplicate_event" in result.issue_tags


def test_non_duplicate_with_different_summary(validator):
    """Unrelated summaries should not trigger duplicate detection."""
    event = _event(summary="AAPL announces new iPhone model release next quarter")
    recent = ["GOOGL reports strong cloud earnings beat estimates"]
    result = validator.validate(event=event, signal=_signal(), recent_event_summaries=recent)
    assert result.novelty != Novelty.DUPLICATE


# ── Event Strength: Noise ─────────────────────────────────────────────────────

def test_noise_headline_rejected(validator):
    """Market round-up headlines should be REJECTED as NOISE."""
    noisy = _event(summary="Stock market today: trending tickers and market movers")
    result = validator.validate(event=noisy, signal=_signal())
    assert result.event_strength == EventStrength.NOISE
    assert result.execution_recommendation == ExecutionRecommendation.REJECT
    assert "noise_headline" in result.issue_tags


def test_sec_filing_excluded_type_rejected(validator):
    """sec_filing event type (excluded from trading) should be REJECTED."""
    e = _event(event_type="sec_filing", summary="Filed 10-K annual report")
    result = validator.validate(event=e, signal=_signal(action="HOLD"))
    assert result.event_strength == EventStrength.NOISE
    assert "excluded_event_type" in result.issue_tags
    assert result.execution_recommendation in (
        ExecutionRecommendation.REJECT,
        ExecutionRecommendation.NO_TRADE,
    )


# ── Event Strength: Weak → DOWNWEIGHT ────────────────────────────────────────

def test_weak_event_downweighted(validator):
    """Low-severity event with low confidence should be DOWNWEIGHT at best."""
    e = _event(
        event_type="buyback",
        severity=55,
        confidence=45,
        summary="Company announces minor share repurchase program",
    )
    result = validator.validate(event=e, signal=_signal(action="BUY"))
    # Weak or noise, never APPROVE
    assert result.execution_recommendation in (
        ExecutionRecommendation.DOWNWEIGHT,
        ExecutionRecommendation.REJECT,
        ExecutionRecommendation.NO_TRADE,
    )


def test_moderate_event_approved_or_downweighted(validator):
    """regulatory_penalty (moderate, not excluded) should be APPROVE or DOWNWEIGHT, not REJECT."""
    e = _event(
        event_type="regulatory_penalty",
        severity=70,
        confidence=72,
        summary="Company faces $50M SEC fine for disclosure violations",
    )
    result = validator.validate(event=e, signal=_signal(action="SHORT"))
    assert result.event_strength in (EventStrength.MODERATE, EventStrength.STRONG)
    assert result.execution_recommendation in (
        ExecutionRecommendation.APPROVE,
        ExecutionRecommendation.DOWNWEIGHT,
    )


# ── Priced-In Risk ────────────────────────────────────────────────────────────

def test_priced_in_high_large_move_before_buy(validator):
    """If ticker already surged 4% on a BUY signal, priced-in risk should be HIGH."""
    ctx = PriceContext(
        intraday_return_pct=0.042,  # +4.2%
        spy_return_pct=0.005,
    )
    result = validator.validate(event=_event(), signal=_signal(action="BUY"), price_context=ctx)
    assert result.priced_in_risk == PricedInRisk.HIGH
    assert "large_move_before_entry" in result.issue_tags


def test_priced_in_high_large_drop_before_short(validator):
    """If ticker already dropped 4% on a SHORT signal, priced-in risk should be HIGH."""
    ctx = PriceContext(
        intraday_return_pct=-0.041,
        spy_return_pct=-0.005,
    )
    result = validator.validate(event=_event(), signal=_signal(action="SHORT"), price_context=ctx)
    assert result.priced_in_risk == PricedInRisk.HIGH
    assert "large_move_before_entry" in result.issue_tags


def test_priced_in_low_small_move(validator):
    """Small intraday move should result in LOW priced-in risk."""
    ctx = PriceContext(
        intraday_return_pct=0.005,
        spy_return_pct=0.003,
    )
    result = validator.validate(event=_event(), signal=_signal(), price_context=ctx)
    assert result.priced_in_risk == PricedInRisk.LOW


def test_near_resistance_flagged(validator):
    """BUY signal when price is 0.5% from resistance should flag near_resistance."""
    ctx = PriceContext(
        intraday_return_pct=0.01,
        pct_to_resistance=0.5,
    )
    result = validator.validate(event=_event(), signal=_signal(action="BUY"), price_context=ctx)
    assert "near_resistance" in result.issue_tags
    assert result.priced_in_risk in (PricedInRisk.MEDIUM, PricedInRisk.HIGH)


# ── Tradeability ──────────────────────────────────────────────────────────────

def test_no_ticker_not_tradeable(validator):
    """Event with no tickers should be NO_TRADE."""
    e = _event(tickers=[])
    s = _signal(ticker="")
    result = validator.validate(event=e, signal=s)
    assert result.tradeability == Tradeability.POOR
    assert "no_ticker" in result.issue_tags
    assert result.execution_recommendation == ExecutionRecommendation.NO_TRADE


def test_very_short_horizon_poor(validator):
    """Horizon < 15 min should result in POOR tradeability."""
    result = validator.validate(event=_event(), signal=_signal(horizon_min=10))
    assert result.tradeability == Tradeability.POOR
    assert "horizon_too_short" in result.issue_tags


# ── Consistency ───────────────────────────────────────────────────────────────

def test_direction_vs_taxonomy_conflict_mixed(validator):
    """BUY signal on a NEGATIVE_EVENT type should flag direction conflict."""
    e = _event(event_type="financial_fraud")
    # financial_fraud is NEGATIVE → expected SHORT; sending BUY is contradictory
    s = _signal(action="BUY")
    result = validator.validate(event=e, signal=s)
    assert result.consistency == Consistency.MIXED
    assert "direction_vs_taxonomy_conflict" in result.issue_tags


def test_fallback_signal_mixed_consistency(validator):
    """Fallback signals should be MIXED consistency."""
    s = _signal(fallback_used=True)
    result = validator.validate(event=_event(), signal=s)
    assert result.consistency == Consistency.MIXED
    assert "fallback_signal" in result.issue_tags


def test_consistent_direction_strong_consistency(validator):
    """SHORT signal on regulatory_penalty (NEGATIVE_EVENT) should be STRONG consistency."""
    e = _event(event_type="regulatory_penalty")
    s = _signal(action="SHORT")
    result = validator.validate(event=e, signal=s)
    assert result.consistency == Consistency.STRONG


# ── Ticker mismatch ───────────────────────────────────────────────────────────

def test_ticker_mismatch_flagged(validator):
    """Signal ticker not in event tickers should add ticker_mismatch tag."""
    e = _event(tickers=["MSFT"])
    s = _signal(ticker="AAPL")
    result = validator.validate(event=e, signal=s)
    assert "ticker_mismatch" in result.issue_tags
    # Should still downweight/reject, not silently approve
    assert result.execution_recommendation != ExecutionRecommendation.APPROVE


# ── Review score range ────────────────────────────────────────────────────────

def test_review_score_bounded(validator):
    """review_score must always be in [0, 100]."""
    for action in ("BUY", "SHORT", "HOLD"):
        for sev in (20, 55, 70, 85):
            e = _event(severity=sev)
            s = _signal(action=action)
            result = validator.validate(event=e, signal=s)
            assert 0 <= result.review_score <= 100, f"score={result.review_score} out of range"


# ── MarketContext placeholder ─────────────────────────────────────────────────

def test_market_context_placeholder_accepted(validator):
    """Passing MarketContext should not crash even though it's not used yet."""
    mc = MarketContext(regime="BULL", vix_level=14.5)
    result = validator.validate(event=_event(), signal=_signal(), market_context=mc)
    assert result is not None


# ── End-to-end: all dims APPROVE ─────────────────────────────────────────────

def test_full_approve_scenario(validator):
    """
    All dims favourable:
    - Fresh event (just now)
    - STRONG event type (regulatory_penalty)
    - Small intraday move (not priced in)
    - Proper ticker and horizon
    - Consistent direction
    → Should APPROVE with high score.
    """
    e = _event(
        event_type="regulatory_penalty",
        tickers=["AAPL"],
        severity=85,
        confidence=80,
        summary="SEC charges Apple with $2bn securities fraud settlement",
        event_time=_now() - timedelta(minutes=5),
    )
    s = _signal(action="SHORT", ticker="AAPL", horizon_min=120)
    ctx = PriceContext(intraday_return_pct=0.002, spy_return_pct=0.001)
    result = validator.validate(event=e, signal=s, price_context=ctx)
    assert result.execution_recommendation == ExecutionRecommendation.APPROVE
    assert result.review_score >= 70
