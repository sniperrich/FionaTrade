from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from app.agents.news_sentiment import NewsSentimentAgent
from app.db.models import Event


def test_news_sentiment_returns_no_signal_when_analysis_context_is_empty(session, settings) -> None:
    agent = NewsSentimentAgent(settings)

    with patch.object(
        agent,
        "_call_llm",
        side_effect=AssertionError("LLM should not be called when news context is empty"),
    ):
        result = agent.analyze(session, "AAPL", {})

    assert result.signal == "NO_SIGNAL"
    assert result.confidence == 0
    assert "No ticker-specific" in result.reasoning
    assert result.metadata["event_count"] == 0
    assert result.metadata["headline_count"] == 0


def test_news_sentiment_still_uses_llm_when_validated_event_exists(session, settings) -> None:
    agent = NewsSentimentAgent(settings)
    session.add(
        Event(
            event_type="major_litigation",
            entities=["AAPL"],
            tickers=["AAPL"],
            severity=80,
            event_time=datetime.now(timezone.utc),
            confidence=88,
            validation_status="VALID",
            summary="AAPL faces major litigation",
        )
    )
    session.flush()

    with patch.object(
        agent,
        "_call_llm",
        return_value=(
            '{"signal":"SHORT","confidence":72,"sentiment":"BEARISH",'
            '"event_strength":"MODERATE","key_catalyst":"litigation risk",'
            '"reasoning":"validated event is bearish","read_full_used":false}'
        ),
    ):
        result = agent.analyze(session, "AAPL", {"as_of": datetime.now(timezone.utc)})

    assert result.signal == "SHORT"
    assert result.confidence == 72
    assert result.metadata["event_count"] == 1
