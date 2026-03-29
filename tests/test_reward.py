"""Tests for the agent reward / penalty scoring engine."""

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.reward import (
    score_prediction,
    score_agent_run,
    get_agent_performance,
    build_performance_context,
    compute_dynamic_weights,
    batch_score_runs,
)
from app.db.models import AgentRun, AgentScore, Bar1m


class TestScorePrediction:
    """Test the core scoring function."""

    def test_correct_buy(self):
        score, reason = score_prediction("BUY", 3.0)
        assert score > 0
        assert "Correct BUY" in reason

    def test_wrong_buy(self):
        score, reason = score_prediction("BUY", -4.0)
        assert score < 0
        assert "Wrong BUY" in reason

    def test_correct_short(self):
        score, reason = score_prediction("SHORT", -5.0)
        assert score > 0
        assert "Correct SHORT" in reason

    def test_wrong_short(self):
        score, reason = score_prediction("SHORT", 3.0)
        assert score < 0
        assert "Wrong SHORT" in reason

    def test_hold_on_flat(self):
        score, reason = score_prediction("HOLD", 0.3)
        assert score > 0
        assert "barely moved" in reason

    def test_hold_missed_big_move(self):
        score, reason = score_prediction("HOLD", 8.0)
        assert score < 0
        assert "missed big" in reason

    def test_buy_flat_neutral(self):
        score, _ = score_prediction("BUY", 0.2)
        assert score >= 0  # small positive or neutral

    def test_scores_bounded(self):
        """Scores should never exceed ±100."""
        score_pos, _ = score_prediction("BUY", 50.0)
        score_neg, _ = score_prediction("BUY", -50.0)
        assert -100 <= score_pos <= 100
        assert -100 <= score_neg <= 100


class TestScoreAgentRun:
    """Test scoring a full AgentRun against actual prices."""

    def test_score_run_with_price_data(self, session):
        # Create bars for AAPL
        base_time = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
        for i in range(5):
            bar = Bar1m(
                ticker="AAPL",
                ts=base_time + timedelta(days=i, minutes=i),
                open=100 + i, high=101 + i, low=99 + i,
                close=100 + i * 2,  # rising price
                volume=1000,
            )
            session.add(bar)
        session.flush()

        # Create an AgentRun with BUY signals
        run = AgentRun(
            ticker="AAPL",
            trigger="test",
            macro_output={"signal": "BUY", "confidence": 70, "reasoning": "bullish"},
            news_output={"signal": "BUY", "confidence": 60, "reasoning": "good news"},
            technicals_output={"signal": "SHORT", "confidence": 50, "reasoning": "overbought"},
            fundamentals_output={"signal": "HOLD", "confidence": 30, "reasoning": "neutral"},
            risk_output={"signal": "BUY", "confidence": 70, "reasoning": "approved"},
            final_action="BUY",
            status="COMPLETED",
            created_at=base_time,
        )
        session.add(run)
        session.flush()

        scores = score_agent_run(session, run, eval_horizon_days=3)
        assert len(scores) >= 3  # macro, news, technicals, risk at least

        # Macro said BUY, price went up → positive score
        macro_score = next(s for s in scores if s.agent_name == "macro_analyst")
        assert macro_score.score > 0

        # Technicals said SHORT, price went up → negative score
        tech_score = next(s for s in scores if s.agent_name == "technicals")
        assert tech_score.score < 0

    def test_no_price_data_returns_empty(self, session):
        run = AgentRun(
            ticker="FAKE",
            trigger="test",
            macro_output={"signal": "BUY", "confidence": 70, "reasoning": "test"},
            final_action="BUY",
            status="COMPLETED",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        session.add(run)
        session.flush()

        scores = score_agent_run(session, run)
        assert len(scores) == 0  # No price data → no scores


class TestGetAgentPerformance:
    def test_no_history_returns_defaults(self, session):
        perf = get_agent_performance(session, "macro_analyst", ticker="AAPL")
        assert perf["total_predictions"] == 0
        assert perf["accuracy_pct"] == 50.0
        assert perf["summary"] == "No prediction history available."

    def test_with_scores(self, session):
        # Insert some scores directly
        for i in range(5):
            s = AgentScore(
                agent_name="news_sentiment",
                ticker="AAPL",
                signal="BUY",
                confidence=70,
                predicted_at=datetime(2026, 2, 10 + i, tzinfo=timezone.utc),
                price_at_prediction=100.0,
                price_after=103.0,
                actual_return_pct=3.0,
                score=42.0,
                score_reasoning="Correct BUY",
            )
            session.add(s)
        session.flush()

        perf = get_agent_performance(
            session, "news_sentiment", ticker="AAPL",
            as_of=datetime(2026, 2, 20, tzinfo=timezone.utc),
        )
        assert perf["total_predictions"] == 5
        assert perf["avg_score"] > 0
        assert perf["hit_rate"] == 100.0


class TestBuildPerformanceContext:
    def test_empty_returns_empty_string(self, session):
        ctx = build_performance_context(session, "macro_analyst", "AAPL")
        assert ctx == ""

    def test_with_history_returns_text(self, session):
        for i in range(3):
            s = AgentScore(
                agent_name="macro_analyst",
                ticker="NVDA",
                signal="SHORT",
                confidence=60,
                predicted_at=datetime(2026, 2, 5 + i, tzinfo=timezone.utc),
                price_at_prediction=180.0,
                price_after=170.0,
                actual_return_pct=-5.5,
                score=33.0,
                score_reasoning="Correct SHORT",
            )
            session.add(s)
        session.flush()

        ctx = build_performance_context(
            session, "macro_analyst", "NVDA",
            as_of=datetime(2026, 2, 20, tzinfo=timezone.utc),
        )
        assert "PAST PERFORMANCE" in ctx
        assert "SHORT" in ctx
        assert "NVDA" in ctx


class TestComputeDynamicWeights:
    def test_no_history_returns_base_weights(self, session):
        weights = compute_dynamic_weights(session)
        assert abs(sum(weights.values()) - 1.0) < 0.01
        assert weights["technicals"] >= 0.10  # base is 0.15

    def test_good_agent_gets_higher_weight(self, session):
        # Give technicals consistently good scores
        for i in range(5):
            s = AgentScore(
                agent_name="technicals",
                ticker="AAPL",
                signal="BUY",
                confidence=80,
                predicted_at=datetime(2026, 2, 5 + i, tzinfo=timezone.utc),
                price_at_prediction=100.0,
                price_after=105.0,
                actual_return_pct=5.0,
                score=80.0,
                score_reasoning="Strong win",
            )
            session.add(s)

        # Give macro consistently bad scores
        for i in range(5):
            s = AgentScore(
                agent_name="macro_analyst",
                ticker="AAPL",
                signal="BUY",
                confidence=70,
                predicted_at=datetime(2026, 2, 5 + i, tzinfo=timezone.utc),
                price_at_prediction=100.0,
                price_after=95.0,
                actual_return_pct=-5.0,
                score=-70.0,
                score_reasoning="Wrong call",
            )
            session.add(s)
        session.flush()

        weights = compute_dynamic_weights(
            session, as_of=datetime(2026, 2, 20, tzinfo=timezone.utc)
        )
        assert weights["technicals"] > weights["macro_analyst"]
        assert abs(sum(weights.values()) - 1.0) < 0.01


def test_batch_score_runs_supports_as_of_and_ready_cutoff(session):
    base = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
    # Bars around prediction time and +2d eval horizon.
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=base,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
            ),
            Bar1m(
                ticker="AAPL",
                ts=base + timedelta(days=2),
                open=103.0,
                high=104.0,
                low=102.0,
                close=103.0,
                volume=1000,
            ),
            Bar1m(
                ticker="AAPL",
                ts=base + timedelta(days=3),
                open=104.0,
                high=105.0,
                low=103.0,
                close=104.0,
                volume=1000,
            ),
        ]
    )
    session.flush()

    ready_run = AgentRun(
        ticker="AAPL",
        trigger="test",
        status="COMPLETED",
        created_at=base,
        macro_output={"signal": "BUY", "confidence": 80, "reasoning": "ready"},
        final_action="BUY",
    )
    too_recent_run = AgentRun(
        ticker="AAPL",
        trigger="test",
        status="COMPLETED",
        created_at=base + timedelta(days=2),
        macro_output={"signal": "BUY", "confidence": 80, "reasoning": "too_recent"},
        final_action="BUY",
    )
    session.add_all([ready_run, too_recent_run])
    session.flush()

    created = batch_score_runs(
        session,
        since=base - timedelta(days=1),
        eval_horizon_days=2,
        as_of=base + timedelta(days=3),
        limit=50,
    )
    session.flush()

    assert created >= 1
    scored_runs = {row.agent_run_id for row in session.query(AgentScore).all()}
    assert ready_run.id in scored_runs
    assert too_recent_run.id not in scored_runs


def test_batch_score_runs_limit_applies_after_excluding_scored_runs(session):
    base = datetime(2026, 2, 12, 15, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=base,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
            ),
            Bar1m(
                ticker="AAPL",
                ts=base + timedelta(days=2),
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.0,
                volume=1000,
            ),
        ]
    )
    session.flush()

    runs: list[AgentRun] = []
    for i in range(4):
        run = AgentRun(
            ticker="AAPL",
            trigger="test",
            status="COMPLETED",
            created_at=base + timedelta(minutes=i),
            macro_output={"signal": "BUY", "confidence": 70, "reasoning": f"run-{i}"},
            final_action="BUY",
        )
        runs.append(run)
    session.add_all(runs)
    session.flush()

    # Pre-score the first two runs so SQL-side exclusion must skip them.
    session.add_all(
        [
            AgentScore(
                agent_run_id=runs[0].id,
                agent_name="macro_analyst",
                ticker="AAPL",
                signal="BUY",
                confidence=70,
                predicted_at=runs[0].created_at,
                price_at_prediction=100.0,
                price_after=102.0,
                actual_return_pct=2.0,
                eval_horizon_days=2,
                score=10.0,
                score_reasoning="pre-scored",
            ),
            AgentScore(
                agent_run_id=runs[1].id,
                agent_name="macro_analyst",
                ticker="AAPL",
                signal="BUY",
                confidence=70,
                predicted_at=runs[1].created_at,
                price_at_prediction=100.0,
                price_after=102.0,
                actual_return_pct=2.0,
                eval_horizon_days=2,
                score=10.0,
                score_reasoning="pre-scored",
            ),
        ]
    )
    session.flush()

    created = batch_score_runs(
        session,
        since=base - timedelta(days=1),
        eval_horizon_days=2,
        as_of=base + timedelta(days=3),
        limit=2,
    )
    session.flush()

    # Each newly selected run emits one macro_analyst score.
    assert created == 2
    newly_scored_run_ids = {
        row.agent_run_id
        for row in session.query(AgentScore).all()
        if row.score_reasoning != "pre-scored"
    }
    assert runs[2].id in newly_scored_run_ids
    assert runs[3].id in newly_scored_run_ids
