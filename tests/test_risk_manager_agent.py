"""Tests for RiskManagerAgent — mocks LLM, tests rule-based pre-checks."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.risk_manager import RiskManagerAgent, _MAX_POSITION_PCT
from app.db.models import Position


def _make_agent(settings):
    return RiskManagerAgent(settings)


def _build_signals(macro="BUY", news="BUY", fund="BUY", tech="BUY", conf=75):
    return {
        "macro_analyst": {"signal": macro, "confidence": conf, "reasoning": "test"},
        "news_sentiment": {"signal": news, "confidence": conf, "reasoning": "test"},
        "fundamentals": {"signal": fund, "confidence": conf, "reasoning": "test"},
        "technicals": {"signal": tech, "confidence": conf, "reasoning": "test"},
    }


class TestRiskManagerHardBlock:
    def test_no_position_no_block(self, settings, session):
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value='{"approved":true,"max_position_pct":0.10,"stop_loss_pct":0.02,"risk_level":"LOW","concerns":[],"reasoning":"OK"}'):
            result = agent.analyze(session, "AAPL", {"agent_signals": _build_signals()})
        assert result.signal != "NO_SIGNAL"
        assert result.metadata.get("hard_block") is False

    def test_oversized_position_hard_blocks(self, settings, session):
        """When position exceeds max, RiskManager must hard-block without calling LLM."""
        # Create a position > 20% of capital
        nav = settings.initial_nav  # e.g. 100_000
        pos = Position(
            ticker="AAPL",
            qty=100,
            avg_price=nav * 0.25,  # 25% of nav → over limit
        )
        session.add(pos)
        session.flush()

        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "AAPL", {"agent_signals": _build_signals()})
        assert result.metadata.get("hard_block") is True
        assert result.metadata.get("approved") is False
        assert result.signal == "HOLD"
        llm_spy.assert_not_called()  # hard block should skip LLM


class TestRiskManagerLLMApproval:
    def test_llm_approved_trade(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = '{"approved":true,"max_position_pct":0.08,"stop_loss_pct":0.02,"risk_level":"MEDIUM","concerns":[],"reasoning":"Looks good"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "GOOGL", {"agent_signals": _build_signals()})
        assert result.metadata.get("approved") is True
        assert result.metadata.get("max_position_pct") <= _MAX_POSITION_PCT

    def test_llm_rejects_trade(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = '{"approved":false,"max_position_pct":0.0,"stop_loss_pct":0.02,"risk_level":"HIGH","concerns":["high VIX"],"reasoning":"Too risky"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "TSLA", {"agent_signals": _build_signals()})
        assert result.metadata.get("approved") is False
        assert result.signal == "HOLD"

    def test_llm_unavailable_fallback(self, settings, session):
        """When LLM is unavailable and consensus exists, approve conservatively."""
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=None):
            result = agent.analyze(session, "MSFT", {"agent_signals": _build_signals()})
        # Should not crash; should return a valid signal
        assert result.signal in ("BUY", "HOLD", "NO_SIGNAL")

    def test_max_position_pct_capped(self, settings, session):
        """LLM cannot suggest more than _MAX_POSITION_PCT regardless of response."""
        agent = _make_agent(settings)
        llm_resp = '{"approved":true,"max_position_pct":0.99,"stop_loss_pct":0.01,"risk_level":"LOW","concerns":[],"reasoning":"YOLO"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "SPY", {"agent_signals": _build_signals()})
        assert result.metadata.get("max_position_pct") <= _MAX_POSITION_PCT


class TestRiskManagerLowConsensus:
    def test_only_one_actionable_signal_noted(self, settings, session):
        """Low consensus should be noted but not hard-block by itself."""
        mixed_signals = {
            "macro_analyst": {"signal": "HOLD", "confidence": 50, "reasoning": "mixed"},
            "news_sentiment": {"signal": "HOLD", "confidence": 50, "reasoning": "nothing"},
            "fundamentals": {"signal": "BUY", "confidence": 60, "reasoning": "ok"},
            "technicals": {"signal": "HOLD", "confidence": 40, "reasoning": "flat"},
        }
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value='{"approved":true,"max_position_pct":0.05,"stop_loss_pct":0.02,"risk_level":"LOW","concerns":[],"reasoning":"Low consensus but ok"}'):
            result = agent.analyze(session, "AAPL", {"agent_signals": mixed_signals})
        # Should still get a valid result (may approve or not, but must not crash)
        assert result.agent_name == "risk_manager"
        assert result.signal in ("BUY", "HOLD", "NO_SIGNAL")
