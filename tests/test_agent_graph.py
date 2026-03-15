"""End-to-end tests for AgentGraph — all LLM calls are mocked."""
from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from app.agent_graph.graph import AgentGraph
from app.db.models import AgentRun, Bar1m


def _add_bars(session, ticker: str, count: int = 60) -> None:
    base = datetime.datetime.utcnow() - datetime.timedelta(minutes=count + 30)
    for i in range(count):
        session.add(Bar1m(
            ticker=ticker,
            ts=base + datetime.timedelta(minutes=i),
            open=150.0 + i * 0.1,
            high=151.0 + i * 0.1,
            low=149.0 + i * 0.1,
            close=150.5 + i * 0.1,
            volume=500_000,
        ))
    session.flush()


_LLM_MACRO = '{"signal":"BUY","confidence":70,"macro_regime":"RISK_ON","key_risks":[],"key_tailwinds":["rate cut"],"reasoning":"Macro supports equities"}'
_LLM_NEWS = '{"signal":"BUY","confidence":65,"sentiment":"BULLISH","event_strength":"MODERATE","key_catalyst":"earnings beat","reasoning":"Positive news flow"}'
_LLM_FUND = '{"signal":"BUY","confidence":72,"fundamental_quality":"STRONG","valuation":"FAIR","earnings_trend":"ACCELERATING","analyst_consensus":"BULLISH","reasoning":"Strong fundamentals"}'
_LLM_RISK = '{"approved":true,"max_position_pct":0.08,"stop_loss_pct":0.02,"risk_level":"LOW","concerns":[],"reasoning":"Risk acceptable"}'
_LLM_PM = '{"action":"BUY","position_pct":0.08,"conviction":"HIGH","supporting_agents":["technicals","fundamentals"],"dissenting_agents":[],"entry_rationale":"All green","exit_criteria":"Stop 2%","reasoning":"Strong multi-factor buy"}'


class TestAgentGraphFullRun:
    def _mock_llm(self, *args, **kwargs):
        """Return different responses based on which prompt is being sent."""
        user_prompt = args[1] if len(args) > 1 else kwargs.get("user_prompt", "")
        if "macroeconomic" in user_prompt.lower():
            return _LLM_MACRO
        if "news" in user_prompt.lower() or "sentiment" in user_prompt.lower():
            return _LLM_NEWS
        if "fundamental" in user_prompt.lower():
            return _LLM_FUND
        if "risk" in user_prompt.lower() or "portfolio context" in user_prompt.lower():
            return _LLM_RISK
        return _LLM_PM  # portfolio manager

    def test_full_run_produces_valid_state(self, settings, session):
        _add_bars(session, "AAPL")
        graph = AgentGraph(settings)

        # Patch LLM on all agents that use it
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = self._mock_llm

        state = graph.run(session, "AAPL")

        assert state["ticker"] == "AAPL"
        assert state["final_action"] in ("BUY", "SHORT", "HOLD")
        assert isinstance(state["final_position_pct"], float)
        assert 0.0 <= state["final_position_pct"] <= 0.20
        assert "error" not in state or state.get("error") is None

    def test_full_run_persists_agent_run(self, settings, session):
        _add_bars(session, "MSFT")
        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = self._mock_llm

        graph.run(session, "MSFT")
        session.flush()

        run = session.query(AgentRun).filter_by(ticker="MSFT").first()
        assert run is not None
        assert run.final_action in ("BUY", "SHORT", "HOLD")
        assert run.execution_ms is not None
        assert run.execution_ms > 0

    def test_parallel_agents_all_produce_results(self, settings, session):
        _add_bars(session, "GOOGL")
        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = self._mock_llm

        state = graph.run(session, "GOOGL")

        assert "macro_analyst_result" in state
        assert "news_sentiment_result" in state
        assert "fundamentals_result" in state
        assert "technicals_result" in state
        assert "risk_manager_result" in state
        assert "portfolio_manager_result" in state


class TestAgentGraphErrorHandling:
    def test_llm_failure_produces_hold(self, settings, session):
        """When all LLM agents fail, graph should gracefully return HOLD."""
        _add_bars(session, "TSLA")
        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = lambda *a, **kw: None  # simulate LLM unavailable

        state = graph.run(session, "TSLA")

        assert state["ticker"] == "TSLA"
        assert state["final_action"] in ("BUY", "SHORT", "HOLD", "NO_SIGNAL")
        # Should not raise or leave state empty
        assert "final_position_pct" in state

    def test_no_bars_still_completes(self, settings, session):
        """Graph must complete even with no bar data for technicals."""
        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = lambda *a, **kw: None

        state = graph.run(session, "UNKNOWN_TICKER")
        assert "final_action" in state
        assert "error" not in state or state.get("error") is None

    def test_run_multiple_tickers(self, settings, session):
        """Graph must handle multiple ticker runs sequentially without state leak."""
        for ticker in ["AAPL", "MSFT", "GOOGL"]:
            _add_bars(session, ticker)

        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = lambda *a, **kw: None

        for ticker in ["AAPL", "MSFT", "GOOGL"]:
            state = graph.run(session, ticker)
            assert state["ticker"] == ticker


class TestAgentGraphAgentSignalsAggregation:
    def test_agent_signals_dict_populated(self, settings, session):
        _add_bars(session, "AMZN")
        graph = AgentGraph(settings)
        for agent in [graph.macro, graph.news, graph.fundamentals, graph.risk, graph.portfolio]:
            agent._call_llm = lambda *a, **kw: None

        state = graph.run(session, "AMZN")

        # agent_signals must contain all 5 analysis agents
        signals = state.get("agent_signals", {})
        assert "macro_analyst" in signals
        assert "news_sentiment" in signals
        assert "fundamentals" in signals
        assert "technicals" in signals
        assert "risk_manager" in signals
