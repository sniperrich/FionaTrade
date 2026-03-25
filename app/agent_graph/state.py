from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """Shared state passed between all nodes in the Agent Graph."""

    # Input
    ticker: str
    context: dict[str, Any]

    # Individual agent outputs (each is the .to_dict() of an AgentSignal)
    macro_analyst_result: dict[str, Any]
    news_sentiment_result: dict[str, Any]
    fundamentals_result: dict[str, Any]
    technicals_result: dict[str, Any]
    risk_manager_result: dict[str, Any]
    portfolio_manager_result: dict[str, Any]

    # Aggregated view passed to downstream agents
    agent_signals: dict[str, dict[str, Any]]

    # Final decision (from portfolio_manager)
    final_action: str      # BUY | SHORT | SELL | HOLD
    final_position_pct: float
    final_reasoning: str
    execution_plan: dict[str, Any]

    # Execution metadata
    error: str | None
