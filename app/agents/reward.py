"""Agent reward / penalty scoring engine.

Evaluates each agent's past predictions against actual price movements and
provides performance feedback that gets injected into agent prompts.

Scoring rules:
  BUY  + price went up   → positive score (scaled by magnitude)
  BUY  + price went down → negative score
  SHORT + price went down → positive score
  SHORT + price went up  → negative score
  HOLD  + small move     → small positive (correctly stayed out)
  HOLD  + big move       → small negative (missed opportunity)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session

from app.core.logging import get_app_logger
from app.db.models import AgentScore, AgentRun, Bar1m

logger = get_app_logger()

# Agent name mapping: agent output key → agent_name in scores
_AGENT_OUTPUT_KEYS = {
    "macro_output": "macro_analyst",
    "news_output": "news_sentiment",
    "fundamentals_output": "fundamentals",
    "technicals_output": "technicals",
    "risk_output": "risk_manager",
}

_DEFAULT_EVAL_HORIZON = 3  # days to evaluate prediction


def score_prediction(signal: str, actual_return_pct: float) -> tuple[float, str]:
    """Score a single prediction.

    Returns (score, reasoning) where score is -100 to +100.
    """
    abs_return = abs(actual_return_pct)

    if signal in ("BUY",):
        if actual_return_pct > 0.5:
            score = min(100, actual_return_pct * 20)
            reason = f"Correct BUY: stock rose {actual_return_pct:+.1f}%"
        elif actual_return_pct > -0.5:
            score = 5.0
            reason = f"BUY on flat stock ({actual_return_pct:+.1f}%), neutral"
        else:
            score = max(-100, actual_return_pct * 20)
            reason = f"Wrong BUY: stock fell {actual_return_pct:+.1f}%"

    elif signal in ("SHORT",):
        inverted = -actual_return_pct
        if inverted > 0.5:
            score = min(100, inverted * 20)
            reason = f"Correct SHORT: stock fell {actual_return_pct:+.1f}%"
        elif inverted > -0.5:
            score = 5.0
            reason = f"SHORT on flat stock ({actual_return_pct:+.1f}%), neutral"
        else:
            score = max(-100, inverted * 20)
            reason = f"Wrong SHORT: stock rose {actual_return_pct:+.1f}%"

    elif signal in ("HOLD", "NO_SIGNAL"):
        if abs_return < 1.0:
            score = 10.0
            reason = f"Correct HOLD: stock barely moved ({actual_return_pct:+.1f}%)"
        elif abs_return < 3.0:
            score = -5.0
            reason = f"HOLD missed moderate move ({actual_return_pct:+.1f}%)"
        else:
            score = -15.0
            reason = f"HOLD missed big move ({actual_return_pct:+.1f}%)"
    else:
        score = 0.0
        reason = f"Unknown signal '{signal}'"

    return round(score, 1), reason


def score_agent_run(
    session: Session,
    agent_run: AgentRun,
    eval_horizon_days: int = _DEFAULT_EVAL_HORIZON,
) -> list[AgentScore]:
    """Score all agents in a single AgentRun against what actually happened.

    Looks at price `eval_horizon_days` after the prediction to evaluate accuracy.
    """
    ticker = agent_run.ticker
    prediction_time = agent_run.created_at
    scores: list[AgentScore] = []

    # Get price at prediction time and N days later
    price_at = _get_price_near(session, ticker, prediction_time)
    eval_time = prediction_time + timedelta(days=eval_horizon_days)
    price_after = _get_price_near(session, ticker, eval_time)

    if not price_at or not price_after or price_at <= 0:
        return scores

    actual_return = (price_after - price_at) / price_at * 100

    for output_key, agent_name in _AGENT_OUTPUT_KEYS.items():
        output = getattr(agent_run, output_key, None)
        if not output:
            continue

        if isinstance(output, str):
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                continue

        signal = output.get("signal", "NO_SIGNAL")
        confidence = output.get("confidence", 0)

        if signal in ("NO_SIGNAL",) and confidence == 0:
            continue  # Don't score agents that produced nothing

        score_val, reasoning = score_prediction(signal, actual_return)
        # Scale score by confidence — high-confidence wrong calls penalized more
        conf_multiplier = confidence / 100.0 if confidence > 0 else 0.5
        adjusted_score = score_val * conf_multiplier

        agent_score = AgentScore(
            agent_run_id=agent_run.id,
            agent_name=agent_name,
            ticker=ticker,
            signal=signal,
            confidence=confidence,
            predicted_at=prediction_time,
            price_at_prediction=price_at,
            price_after=price_after,
            actual_return_pct=round(actual_return, 2),
            eval_horizon_days=eval_horizon_days,
            score=round(adjusted_score, 1),
            score_reasoning=reasoning,
        )
        scores.append(agent_score)

    return scores


def batch_score_runs(
    session: Session,
    since: datetime | None = None,
    eval_horizon_days: int = _DEFAULT_EVAL_HORIZON,
) -> int:
    """Score all unscored AgentRuns. Returns count of new scores created."""
    # Find runs that haven't been scored yet
    already_scored = select(AgentScore.agent_run_id).distinct()
    query = select(AgentRun).where(
        AgentRun.status == "COMPLETED",
        AgentRun.id.notin_(already_scored),
    )
    if since:
        query = query.where(AgentRun.created_at >= since)

    runs = session.execute(query.order_by(AgentRun.created_at)).scalars().all()
    total_scored = 0

    for run in runs:
        scores = score_agent_run(session, run, eval_horizon_days)
        for s in scores:
            session.add(s)
            total_scored += 1

    if total_scored:
        session.flush()
        logger.info("[reward] Scored %d agent predictions across %d runs", total_scored, len(runs))

    return total_scored


def get_agent_performance(
    session: Session,
    agent_name: str,
    ticker: str | None = None,
    lookback_days: int = 30,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Get aggregated performance stats for an agent.

    Returns dict with: avg_score, total_predictions, accuracy_pct, recent_scores
    """
    ref_time = as_of or datetime.now(timezone.utc)
    cutoff = ref_time - timedelta(days=lookback_days)

    query = select(AgentScore).where(
        AgentScore.agent_name == agent_name,
        AgentScore.predicted_at >= cutoff,
        AgentScore.predicted_at <= ref_time,
    )
    if ticker:
        query = query.where(AgentScore.ticker == ticker)

    scores = session.execute(query.order_by(AgentScore.predicted_at.desc())).scalars().all()

    if not scores:
        return {
            "agent_name": agent_name,
            "avg_score": 0.0,
            "total_predictions": 0,
            "accuracy_pct": 50.0,  # default neutral
            "hit_rate": 0.0,
            "recent_scores": [],
            "summary": "No prediction history available.",
        }

    total = len(scores)
    avg_score = sum(s.score for s in scores) / total
    correct = sum(1 for s in scores if s.score > 0)
    hit_rate = correct / total * 100

    recent = [
        {
            "ticker": s.ticker,
            "signal": s.signal,
            "actual_return": s.actual_return_pct,
            "score": s.score,
            "date": s.predicted_at.strftime("%m-%d"),
        }
        for s in scores[:10]
    ]

    # Convert avg_score to 0-100 accuracy scale
    accuracy_pct = max(0, min(100, 50 + avg_score))

    return {
        "agent_name": agent_name,
        "avg_score": round(avg_score, 1),
        "total_predictions": total,
        "accuracy_pct": round(accuracy_pct, 1),
        "hit_rate": round(hit_rate, 1),
        "recent_scores": recent,
        "summary": _build_performance_summary(agent_name, avg_score, hit_rate, total, recent),
    }


def build_performance_context(
    session: Session,
    agent_name: str,
    ticker: str,
    as_of: datetime | None = None,
) -> str:
    """Build a text block for injection into an agent's prompt, showing its track record."""
    perf = get_agent_performance(session, agent_name, ticker=ticker, lookback_days=30, as_of=as_of)
    overall = get_agent_performance(session, agent_name, ticker=None, lookback_days=30, as_of=as_of)

    if perf["total_predictions"] == 0 and overall["total_predictions"] == 0:
        return ""  # No history, don't clutter the prompt

    lines = ["YOUR PAST PERFORMANCE (use this to calibrate your confidence):"]

    if perf["total_predictions"] > 0:
        lines.append(
            f"  On {ticker}: {perf['total_predictions']} predictions, "
            f"{perf['hit_rate']:.0f}% hit rate, avg score {perf['avg_score']:+.1f}"
        )
        for r in perf["recent_scores"][:5]:
            lines.append(
                f"    {r['date']}: You said {r['signal']}, stock moved {r['actual_return']:+.1f}% → score {r['score']:+.0f}"
            )

    if overall["total_predictions"] > perf["total_predictions"]:
        lines.append(
            f"  Overall: {overall['total_predictions']} predictions, "
            f"{overall['hit_rate']:.0f}% hit rate, avg score {overall['avg_score']:+.1f}"
        )

    if perf["avg_score"] < -10:
        lines.append(
            "  ⚠️ Your recent predictions have been POOR. Be more cautious and reconsider your approach."
        )
    elif perf["avg_score"] > 20:
        lines.append(
            "  ✅ Your recent predictions have been GOOD. Maintain your current analytical approach."
        )

    return "\n".join(lines)


def compute_dynamic_weights(
    session: Session,
    as_of: datetime | None = None,
) -> dict[str, float]:
    """Compute dynamic agent weights based on recent performance.

    Base weights: technicals=35%, news=25%, fundamentals=20%, macro=20%
    Adjusted ±10% based on recent scores. Weights always sum to 1.0.
    """
    base_weights = {
        "technicals": 0.35,
        "news_sentiment": 0.25,
        "fundamentals": 0.20,
        "macro_analyst": 0.20,
    }

    adjustments = {}
    for agent_name, base_w in base_weights.items():
        perf = get_agent_performance(session, agent_name, lookback_days=14, as_of=as_of)
        if perf["total_predictions"] < 3:
            adjustments[agent_name] = 0.0
            continue

        # Scale adjustment: avg_score of +50 → +10% weight, -50 → -10% weight
        adj = perf["avg_score"] / 500.0  # ±0.10 max
        adj = max(-0.10, min(0.10, adj))
        adjustments[agent_name] = adj

    # Apply adjustments and renormalize
    adjusted = {
        name: max(0.05, base_weights[name] + adjustments.get(name, 0))
        for name in base_weights
    }
    total = sum(adjusted.values())
    return {name: round(w / total, 3) for name, w in adjusted.items()}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_price_near(session: Session, ticker: str, target_time: datetime) -> float | None:
    """Get the closest bar price to a target timestamp."""
    bar = session.execute(
        select(Bar1m)
        .where(
            Bar1m.ticker == ticker.upper(),
            Bar1m.ts >= target_time - timedelta(hours=12),
            Bar1m.ts <= target_time + timedelta(hours=12),
        )
        .order_by(func.abs(func.julianday(Bar1m.ts) - func.julianday(target_time)))
        .limit(1)
    ).scalars().first()
    return float(bar.close) if bar else None


def _build_performance_summary(
    agent_name: str,
    avg_score: float,
    hit_rate: float,
    total: int,
    recent: list[dict],
) -> str:
    if total == 0:
        return "No prediction history."

    grade = (
        "excellent" if avg_score > 30
        else "good" if avg_score > 10
        else "average" if avg_score > -10
        else "poor" if avg_score > -30
        else "very poor"
    )
    return (
        f"{agent_name}: {total} predictions, {hit_rate:.0f}% hit rate, "
        f"avg score {avg_score:+.1f} ({grade})"
    )
