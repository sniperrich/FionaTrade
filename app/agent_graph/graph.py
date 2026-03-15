from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.agent_graph.state import AgentState
from app.agents.fundamentals import FundamentalsAgent
from app.agents.macro_analyst import MacroAnalystAgent
from app.agents.news_sentiment import NewsSentimentAgent
from app.agents.portfolio_manager import PortfolioManagerAgent
from app.agents.risk_manager import RiskManagerAgent
from app.agents.technicals import TechnicalsAgent
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.db.models import AgentRun

logger = get_app_logger()


class AgentGraph:
    """
    Multi-agent decision graph for FionaTrade.

    Execution order:
        1. [parallel]  MacroAnalyst, NewsSentiment, Fundamentals, Technicals
        2. [serial]    RiskManager   (reads all 4 above)
        3. [serial]    PortfolioManager  (reads all 5 above)
    """

    def __init__(self, settings: Settings, session_factory: sessionmaker | None = None) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self.macro = MacroAnalystAgent(settings)
        self.news = NewsSentimentAgent(settings)
        self.fundamentals = FundamentalsAgent(settings)
        self.technicals = TechnicalsAgent(settings)
        self.risk = RiskManagerAgent(settings)
        self.portfolio = PortfolioManagerAgent(settings)

    # ── Graph nodes ────────────────────────────────────────────────────────

    def _run_parallel_agents(self, session: Session, state: AgentState) -> AgentState:
        """Run Macro, News, Fundamentals, Technicals concurrently.
        
        Each thread gets its own DB session to avoid SQLAlchemy
        'concurrent operations' errors with shared sessions.
        """
        ticker = state["ticker"]
        context = state.get("context", {})

        # Use injected session_factory or fall back to production SessionLocal
        if self._session_factory is not None:
            make_session = self._session_factory
        else:
            from app.db.database import SessionLocal
            make_session = SessionLocal

        parallel_agents = [
            (self.macro, "macro_analyst_result"),
            (self.news, "news_sentiment_result"),
            (self.fundamentals, "fundamentals_result"),
            (self.technicals, "technicals_result"),
        ]

        def _run_agent_with_own_session(agent, result_key):
            """Run a single agent in its own DB session."""
            thread_session = make_session()
            try:
                return result_key, agent.analyze(thread_session, ticker, context)
            finally:
                thread_session.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_key = {
                pool.submit(_run_agent_with_own_session, agent, result_key): result_key
                for agent, result_key in parallel_agents
            }
            for future in as_completed(future_to_key):
                result_key = future_to_key[future]
                try:
                    _, result = future.result(timeout=60)
                    state[result_key] = result.to_dict()
                except Exception as exc:
                    logger.warning("[graph] %s failed: %s", result_key, exc)
                    state[result_key] = {
                        "agent_name": result_key.replace("_result", ""),
                        "signal": "NO_SIGNAL",
                        "confidence": 0,
                        "reasoning": f"Agent failed: {exc}",
                        "error": str(exc),
                    }

        # Build aggregated signals dict for downstream agents
        state["agent_signals"] = {
            "macro_analyst": state.get("macro_analyst_result", {}),
            "news_sentiment": state.get("news_sentiment_result", {}),
            "fundamentals": state.get("fundamentals_result", {}),
            "technicals": state.get("technicals_result", {}),
        }
        return state

    def _run_risk_manager(self, session: Session, state: AgentState) -> AgentState:
        ticker = state["ticker"]
        context = {**(state.get("context") or {}), "agent_signals": state.get("agent_signals", {})}
        result = self.risk.analyze(session, ticker, context)
        state["risk_manager_result"] = result.to_dict()
        # Merge risk result into agent_signals for portfolio manager
        state["agent_signals"]["risk_manager"] = result.to_dict()
        return state

    def _run_portfolio_manager(self, session: Session, state: AgentState) -> AgentState:
        ticker = state["ticker"]
        context = {**(state.get("context") or {}), "agent_signals": state.get("agent_signals", {})}
        result = self.portfolio.analyze(session, ticker, context)
        state["portfolio_manager_result"] = result.to_dict()

        meta = result.metadata or {}
        state["final_action"] = meta.get("action", result.signal)
        state["final_position_pct"] = meta.get("position_pct", 0.0)
        state["final_reasoning"] = result.reasoning
        return state

    # ── Public run method ──────────────────────────────────────────────────

    def run(self, session: Session, ticker: str, context: dict | None = None) -> AgentState:
        """Execute the full agent graph for a ticker. Returns final AgentState."""
        state: AgentState = {
            "ticker": ticker.upper(),
            "context": context or {},
            "agent_signals": {},
        }
        start_time = time.perf_counter()

        try:
            state = self._run_parallel_agents(session, state)
            state = self._run_risk_manager(session, state)
            state = self._run_portfolio_manager(session, state)
        except Exception as exc:
            logger.exception("[graph] Unhandled error running agent graph for %s: %s", ticker, exc)
            state["error"] = str(exc)
            state.setdefault("final_action", "HOLD")
            state.setdefault("final_position_pct", 0.0)
            state.setdefault("final_reasoning", f"Graph error: {exc}")

        elapsed = time.perf_counter() - start_time
        self._persist_run(session, ticker, state, elapsed)
        logger.info(
            "[graph] %s → action=%s position=%.1f%% (%.1fs)",
            ticker,
            state.get("final_action", "HOLD"),
            float(state.get("final_position_pct", 0.0)) * 100,
            elapsed,
        )
        return state

    def _persist_run(
        self, session: Session, ticker: str, state: AgentState, elapsed: float
    ) -> None:
        """Store a summary of this run in the AgentRun table."""
        try:
            run = AgentRun(
                ticker=ticker.upper(),
                trigger="scheduled",
                macro_output=state.get("macro_analyst_result") or {},
                news_output=state.get("news_sentiment_result") or {},
                fundamentals_output=state.get("fundamentals_result") or {},
                technicals_output=state.get("technicals_result") or {},
                risk_output=state.get("risk_manager_result") or {},
                portfolio_output=state.get("portfolio_manager_result") or {},
                final_action=state.get("final_action", "HOLD"),
                final_position_pct=state.get("final_position_pct", 0.0),
                final_reasoning=state.get("final_reasoning", ""),
                execution_ms=int(elapsed * 1000),
                status="FAILED" if state.get("error") else "COMPLETED",
                error_message=state.get("error"),
            )
            session.add(run)
            session.flush()
        except Exception as exc:
            logger.warning("[graph] Failed to persist AgentRun for %s: %s", ticker, exc)
