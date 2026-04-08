"""Tests for PortfolioManagerAgent — mocks LLM, validates final decision logic."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents.portfolio_manager import PortfolioManagerAgent


def _make_agent(settings):
    return PortfolioManagerAgent(settings)


def _build_context(
    macro_signal="BUY", news_signal="BUY", fund_signal="BUY", tech_signal="BUY",
    risk_approved=True, max_pct=0.10, risk_level="LOW", hard_block=False
):
    return {
        "agent_signals": {
            "macro_analyst": {"signal": macro_signal, "confidence": 70, "reasoning": "macro ok"},
            "news_sentiment": {"signal": news_signal, "confidence": 65, "reasoning": "news ok"},
            "fundamentals": {"signal": fund_signal, "confidence": 72, "reasoning": "fundamentals ok"},
            "technicals": {"signal": tech_signal, "confidence": 80, "reasoning": "technicals ok"},
            "risk_manager": {
                "signal": "HOLD" if not risk_approved else "BUY",
                "confidence": 70,
                "reasoning": "risk ok" if risk_approved else "blocked",
                "metadata": {
                    "approved": risk_approved,
                    "max_position_pct": max_pct,
                    "risk_level": risk_level,
                    "hard_block": hard_block,
                },
            },
        }
    }


class TestPortfolioManagerBuyDecision:
    def test_all_buy_signals_returns_buy(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = '{"action":"BUY","position_pct":0.08,"conviction":"HIGH","supporting_agents":["technicals","fundamentals"],"dissenting_agents":[],"entry_rationale":"Strong buy","exit_criteria":"Stop 2%","reasoning":"All signals aligned"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "AAPL", _build_context())
        assert result.signal == "BUY"
        assert result.metadata["position_pct"] <= 0.10
        assert result.metadata["conviction"] == "HIGH"

    def test_position_pct_capped_by_risk_manager(self, settings, session):
        agent = _make_agent(settings)
        # LLM suggests 0.20 but risk max is 0.05
        llm_resp = '{"action":"BUY","position_pct":0.20,"conviction":"HIGH","supporting_agents":[],"dissenting_agents":[],"entry_rationale":"big bet","exit_criteria":"hold","reasoning":"Go big"}'
        context = _build_context(max_pct=0.05)
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "NVDA", context)
        assert result.metadata["position_pct"] <= 0.05


class TestPortfolioManagerHoldDecision:
    def test_risk_not_approved_forces_hold(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = '{"action":"BUY","position_pct":0.10,"conviction":"HIGH","supporting_agents":[],"dissenting_agents":[],"entry_rationale":"buy","exit_criteria":"stop","reasoning":"buy"}'
        context = _build_context(risk_approved=False, max_pct=0.0)
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "TSLA", context)
        assert result.signal == "HOLD"
        assert result.metadata["position_pct"] == 0.0

    def test_hard_block_skips_llm(self, settings, session):
        """If risk manager hard-blocked, portfolio manager should not call LLM."""
        agent = _make_agent(settings)
        from unittest.mock import MagicMock
        llm_spy = MagicMock()
        agent._call_llm = llm_spy
        context = _build_context(risk_approved=False, hard_block=True, max_pct=0.0)
        result = agent.analyze(session, "GME", context)
        assert result.signal == "HOLD"
        llm_spy.assert_not_called()

    def test_llm_unavailable_returns_no_signal(self, settings, session):
        agent = _make_agent(settings)
        with patch.object(agent, "_call_llm", return_value=None):
            result = agent.analyze(session, "META", _build_context())
        assert result.signal == "NO_SIGNAL"

    def test_wait_execution_plan_preserves_planned_position(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = (
            '{"action":"BUY","position_pct":0.07,"execution_mode":"WAIT_PULLBACK",'
            '"planned_action":"BUY","valid_for_minutes":180,"entry_plan":{"pullback_pct":0.6},'
            '"conviction":"MEDIUM","supporting_agents":["news_sentiment","macro_analyst"],'
            '"dissenting_agents":["technicals"],"entry_rationale":"wait pullback",'
            '"exit_criteria":"stop","reasoning":"good catalyst but entry stretched"}'
        )
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "AAPL", _build_context())

        assert result.signal == "HOLD"
        assert result.metadata["position_pct"] == 0.0
        plan = result.metadata["execution_plan"]
        assert plan["execution_mode"] == "WAIT_PULLBACK"
        assert plan["planned_action"] == "BUY"
        assert abs(float(plan["planned_position_pct"]) - 0.07) < 1e-9

    def test_news_priority_blocks_short_against_bullish_news(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = (
            '{"action":"SHORT","position_pct":0.08,"conviction":"MEDIUM",'
            '"supporting_agents":["macro_analyst","technicals"],"dissenting_agents":["news_sentiment"],'
            '"entry_rationale":"risk-off","exit_criteria":"stop","reasoning":"macro and technicals bearish"}'
        )
        context = _build_context(
            macro_signal="SHORT",
            news_signal="BUY",
            fund_signal="HOLD",
            tech_signal="SHORT",
        )
        context["agent_signals"]["news_sentiment"]["confidence"] = 70
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "XOM", context)

        assert result.signal == "HOLD"
        assert result.metadata["position_pct"] == 0.0
        assert "news_priority_gate" in result.reasoning


class TestPortfolioManagerWeightedConfidence:
    def test_weighted_confidence_calculation(self, settings, session):
        """Test the weighted confidence helper."""
        agent = _make_agent(settings)
        signals = {
            "technicals": {"signal": "BUY", "confidence": 80},
            "news_sentiment": {"signal": "BUY", "confidence": 60},
            "fundamentals": {"signal": "BUY", "confidence": 70},
            "macro_analyst": {"signal": "BUY", "confidence": 50},
        }
        # 80*0.10 + 60*0.80 + 70*0.05 + 50*0.05 = 8+48+3.5+2.5 = 62.0
        conf = agent._compute_weighted_confidence(signals)
        assert abs(conf - 62.0) < 0.1

    def test_no_signals_returns_fifty(self, settings, session):
        agent = _make_agent(settings)
        conf = agent._compute_weighted_confidence({})
        assert conf == 50.0

    def test_no_signal_type_excluded(self, settings, session):
        agent = _make_agent(settings)
        signals = {
            "technicals": {"signal": "NO_SIGNAL", "confidence": 90},
            "news_sentiment": {"signal": "BUY", "confidence": 60},
        }
        # Only news_sentiment counts (single-signal path re-normalizes to 100%).
        conf = agent._compute_weighted_confidence(signals)
        assert abs(conf - 60.0) < 0.1

    def test_macro_relevance_scales_macro_weight(self, settings, session):
        agent = _make_agent(settings)
        base = agent._base_weights()
        adjusted = agent._contextual_weights(
            base,
            {"metadata": {"ticker_relevance": 0.10}, "reasoning": "oil shock"},
        )
        assert adjusted["macro_analyst"] < base["macro_analyst"]
        assert adjusted["news_sentiment"] > base["news_sentiment"]


class TestPortfolioManagerInvalidLLMResponse:
    def test_invalid_action_defaults_to_hold(self, settings, session):
        agent = _make_agent(settings)
        llm_resp = '{"action":"ROCKET","position_pct":0.05,"conviction":"HIGH","supporting_agents":[],"dissenting_agents":[],"entry_rationale":"x","exit_criteria":"y","reasoning":"bad"}'
        with patch.object(agent, "_call_llm", return_value=llm_resp):
            result = agent.analyze(session, "AAPL", _build_context())
        assert result.signal == "HOLD"
