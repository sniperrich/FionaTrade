"""Tests for RiskManagerAgent — mocks LLM, tests rule-based pre-checks."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.risk_manager import (
    RiskManagerAgent, _MAX_POSITION_PCT, _MIN_CONSENSUS_COUNT,
    _MAX_SAME_DIRECTION, _TICKER_MAX_CONSECUTIVE_LOSSES,
)
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


_LLM_APPROVE = '{"approved":true,"max_position_pct":0.10,"stop_loss_pct":0.05,"risk_level":"LOW","concerns":[],"reasoning":"OK"}'


class TestRiskManagerHardBlock:
    def test_no_position_no_block(self, settings, session):
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=_LLM_APPROVE):
            result = agent.analyze(session, "AAPL", {"agent_signals": _build_signals()})
        assert result.signal != "NO_SIGNAL"
        assert result.metadata.get("hard_block") is False

    def test_oversized_position_hard_blocks(self, settings, session):
        """When position exceeds max, RiskManager must hard-block without calling LLM."""
        nav = settings.initial_nav
        pos = Position(ticker="AAPL", qty=100, avg_price=nav * 0.25)
        session.add(pos)
        session.flush()

        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "AAPL", {"agent_signals": _build_signals()})
        assert result.metadata.get("hard_block") is True
        assert result.metadata.get("approved") is False
        assert result.signal == "HOLD"
        llm_spy.assert_not_called()


class TestRiskManagerConsensus:
    def test_weak_consensus_blocks(self, settings, session):
        """Only 1 BUY signal should be blocked (need 2+)."""
        weak_signals = {
            "macro_analyst": {"signal": "HOLD", "confidence": 50, "reasoning": "mixed"},
            "news_sentiment": {"signal": "HOLD", "confidence": 50, "reasoning": "nothing"},
            "fundamentals": {"signal": "BUY", "confidence": 60, "reasoning": "ok"},
            "technicals": {"signal": "HOLD", "confidence": 40, "reasoning": "flat"},
        }
        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "AAPL", {"agent_signals": weak_signals})
        assert result.metadata.get("hard_block") is True
        assert result.metadata.get("approved") is False
        llm_spy.assert_not_called()

    def test_strong_consensus_passes(self, settings, session):
        """2+ BUY signals should pass consensus check."""
        strong_signals = _build_signals(macro="BUY", news="BUY", fund="HOLD", tech="HOLD")
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=_LLM_APPROVE):
            result = agent.analyze(session, "AAPL", {"agent_signals": strong_signals})
        assert result.metadata.get("hard_block") is False


class TestRiskManagerConcentration:
    def test_concentration_limit_blocks_long(self, settings, session):
        """Block new LONG when already at max same-direction positions."""
        context = {
            "agent_signals": _build_signals(),
            "portfolio_positions": {
                "AAPL": {"side": "LONG", "shares": 10, "entry": 200},
                "NVDA": {"side": "LONG", "shares": 20, "entry": 150},
                "JPM": {"side": "LONG", "shares": 15, "entry": 300},
            },
        }
        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "XOM", context)
        assert result.metadata.get("hard_block") is True
        assert "Concentration" in result.reasoning
        llm_spy.assert_not_called()

    def test_concentration_ok_when_mixed(self, settings, session):
        """Should allow when directions are mixed."""
        context = {
            "agent_signals": _build_signals(),
            "portfolio_positions": {
                "AAPL": {"side": "LONG", "shares": 10, "entry": 200},
                "NVDA": {"side": "SHORT", "shares": 20, "entry": 150},
            },
        }
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=_LLM_APPROVE):
            result = agent.analyze(session, "XOM", context)
        assert result.metadata.get("hard_block") is False


class TestRiskManagerLossStreak:
    def test_loss_streak_blocks_ticker(self, settings, session):
        """Block ticker after N consecutive losses."""
        context = {
            "agent_signals": _build_signals(),
            "ticker_loss_streak": {"NVDA": _TICKER_MAX_CONSECUTIVE_LOSSES},
        }
        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "NVDA", context)
        assert result.metadata.get("hard_block") is True
        assert "losing streak" in result.reasoning
        llm_spy.assert_not_called()

    def test_no_loss_streak_ok(self, settings, session):
        """Ticker with 0 losses should not be blocked."""
        context = {
            "agent_signals": _build_signals(),
            "ticker_loss_streak": {"NVDA": 1},
        }
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=_LLM_APPROVE):
            result = agent.analyze(session, "NVDA", context)
        assert result.metadata.get("hard_block") is False


class TestRiskManagerDrawdown:
    def test_high_drawdown_blocks(self, settings, session):
        """Block all trading when portfolio drawdown exceeds 5%."""
        context = {
            "agent_signals": _build_signals(),
            "portfolio_state": {
                "position_pct": 0.05,
                "daily_pnl": 0,
                "equity": 94000,
                "current_side": None,
                "drawdown_pct": 0.06,
            },
        }
        agent = _make_agent(settings)
        llm_spy = MagicMock()
        agent._call_llm = llm_spy

        result = agent.analyze(session, "AAPL", context)
        assert result.metadata.get("hard_block") is True
        assert "drawdown" in result.reasoning.lower()
        llm_spy.assert_not_called()


class TestRiskManagerLLMApproval:
    def test_llm_approved_trade(self, settings, session):
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=_LLM_APPROVE):
            result = agent.analyze(session, "GOOGL", {"agent_signals": _build_signals()})
        assert result.metadata.get("approved") is True
        assert result.metadata.get("max_position_pct") <= _MAX_POSITION_PCT

    def test_llm_rejects_trade(self, settings, session):
        agent = _make_agent(settings)
        # Use exactly 2-agent consensus so it goes to LLM (not auto-approved)
        two_signals = _build_signals(macro="BUY", news="BUY", fund="HOLD", tech="HOLD")
        llm_resp = '{"approved":false,"max_position_pct":0.0,"stop_loss_pct":0.05,"risk_level":"HIGH","concerns":["high VIX"],"reasoning":"Too risky"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "TSLA", {"agent_signals": two_signals})
        assert result.metadata.get("approved") is False
        assert result.signal == "HOLD"

    def test_llm_unavailable_fallback(self, settings, session):
        """When LLM is unavailable and consensus exists, approve conservatively."""
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=None):
            result = agent.analyze(session, "MSFT", {"agent_signals": _build_signals()})
        assert result.signal in ("BUY", "HOLD", "NO_SIGNAL")

    def test_max_position_pct_capped(self, settings, session):
        """LLM cannot suggest more than _MAX_POSITION_PCT regardless of response."""
        agent = _make_agent(settings)
        llm_resp = '{"approved":true,"max_position_pct":0.99,"stop_loss_pct":0.01,"risk_level":"LOW","concerns":[],"reasoning":"YOLO"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "SPY", {"agent_signals": _build_signals()})
        assert result.metadata.get("max_position_pct") <= _MAX_POSITION_PCT
