from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.agent_graph.state import AgentState
from app.agents.fundamentals import FundamentalsAgent
from app.agents.macro_analyst import MacroAnalystAgent
from app.agents.news_sentiment import NewsSentimentAgent
from app.agents.portfolio_manager import PortfolioManagerAgent
from app.agents.reward import build_performance_context, compute_dynamic_weights, batch_score_runs
from app.agents.risk_manager import RiskManagerAgent
from app.agents.technicals import TechnicalsAgent
from app.core.config import Settings
from app.core.logging import get_app_logger, log_agent_run
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

    def _run_parallel_agents(
        self,
        session: Session,
        state: AgentState,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentState:
        """Run Macro, News, Fundamentals, Technicals concurrently.
        
        Each thread gets its own DB session to avoid SQLAlchemy
        'concurrent operations' errors with shared sessions.
        """
        ticker = state["ticker"]
        context = state.get("context", {})
        as_of = context.get("as_of")

        # Score any unscored past runs to keep performance data fresh
        try:
            batch_score_runs(session, as_of=as_of)
        except Exception as exc:
            logger.debug("[graph] batch_score_runs failed (non-critical): %s", exc)

        # Build per-agent performance context
        agent_perf_ctx = {}
        for agent_name in ("macro_analyst", "news_sentiment", "fundamentals", "technicals"):
            try:
                perf_text = build_performance_context(session, agent_name, ticker, as_of=as_of)
                if perf_text:
                    agent_perf_ctx[agent_name] = perf_text
            except Exception:
                pass
        context["agent_performance"] = agent_perf_ctx

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
            if progress_callback:
                progress_callback(
                    {
                        "stage": "parallel_agent_running",
                        "agent": agent.name,
                        "message": f"{ticker}: {agent.name} analyzing",
                    }
                )
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
                    if progress_callback:
                        progress_callback(
                            {
                                "stage": "parallel_agent_completed",
                                "agent": state[result_key].get("agent_name"),
                                "message": f"{ticker}: {state[result_key].get('agent_name')} -> {state[result_key].get('signal')}",
                            }
                        )
                except Exception as exc:
                    logger.warning("[graph] %s failed: %s", result_key, exc)
                    state[result_key] = {
                        "agent_name": result_key.replace("_result", ""),
                        "signal": "NO_SIGNAL",
                        "confidence": 0,
                        "reasoning": f"Agent failed: {exc}",
                        "error": str(exc),
                    }
                    if progress_callback:
                        progress_callback(
                            {
                                "stage": "parallel_agent_failed",
                                "agent": result_key.replace("_result", ""),
                                "message": f"{ticker}: {result_key.replace('_result', '')} failed",
                            }
                        )

        # Build aggregated signals dict for downstream agents
        state["agent_signals"] = {
            "macro_analyst": state.get("macro_analyst_result", {}),
            "news_sentiment": state.get("news_sentiment_result", {}),
            "fundamentals": state.get("fundamentals_result", {}),
            "technicals": state.get("technicals_result", {}),
        }
        return state

    def _run_risk_manager(
        self,
        session: Session,
        state: AgentState,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentState:
        ticker = state["ticker"]
        context = {**(state.get("context") or {}), "agent_signals": state.get("agent_signals", {})}
        if progress_callback:
            progress_callback({"stage": "risk_manager_running", "agent": "risk_manager", "message": f"{ticker}: risk manager reviewing"})
        result = self.risk.analyze(session, ticker, context)
        state["risk_manager_result"] = result.to_dict()
        # Merge risk result into agent_signals for portfolio manager
        state["agent_signals"]["risk_manager"] = result.to_dict()
        if progress_callback:
            approved = bool((result.metadata or {}).get("approved"))
            progress_callback({"stage": "risk_manager_completed", "agent": "risk_manager", "message": f"{ticker}: risk manager -> {'approved' if approved else 'blocked'}"})
        return state

    def _run_portfolio_manager(
        self,
        session: Session,
        state: AgentState,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentState:
        ticker = state["ticker"]
        context = {**(state.get("context") or {}), "agent_signals": state.get("agent_signals", {})}
        if progress_callback:
            progress_callback({"stage": "portfolio_manager_running", "agent": "portfolio_manager", "message": f"{ticker}: portfolio manager deciding"})
        result = self.portfolio.analyze(session, ticker, context)
        state["portfolio_manager_result"] = result.to_dict()

        meta = result.metadata or {}
        state["final_action"] = meta.get("action", result.signal)
        state["final_position_pct"] = meta.get("position_pct", 0.0)
        state["final_reasoning"] = result.reasoning
        state["execution_plan"] = dict(meta.get("execution_plan") or {})
        if progress_callback:
            progress_callback({"stage": "portfolio_manager_completed", "agent": "portfolio_manager", "message": f"{ticker}: final {state['final_action']} {state['final_position_pct'] * 100:.1f}%"})
        return state

    # ── Public run method ──────────────────────────────────────────────────

    def run(
        self,
        session: Session,
        ticker: str,
        context: dict | None = None,
        as_of: datetime | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentState:
        """Execute the full agent graph for a ticker. Returns final AgentState.

        Args:
            as_of: Simulation timestamp for backtesting. When set, all data
                   queries are restricted to data available before this time,
                   preventing look-ahead bias.
        """
        from app.core.market_hours import market_session_info

        ctx = dict(context or {})
        if as_of is not None:
            ctx["as_of"] = as_of
        else:
            # Live mode: inject current US market session info so agents know
            # what time it is and whether the market is open.
            if "market_time" not in ctx:
                msi = market_session_info()
                ctx["market_time"] = msi["context_string"]
                ctx["market_session"] = msi["label"]
                ctx["market_et_time"] = msi["et_time_str"]
        state: AgentState = {
            "ticker": ticker.upper(),
            "context": ctx,
            "agent_signals": {},
        }
        start_time = time.perf_counter()

        try:
            if progress_callback:
                progress_callback({"stage": "parallel_start", "agent": "agent_graph", "message": f"{ticker}: starting parallel agent pass"})
            state = self._run_parallel_agents(session, state, progress_callback=progress_callback)
            state = self._run_risk_manager(session, state, progress_callback=progress_callback)
            state = self._run_portfolio_manager(session, state, progress_callback=progress_callback)
        except Exception as exc:
            logger.exception("[graph] Unhandled error running agent graph for %s: %s", ticker, exc)
            state["error"] = str(exc)
            state.setdefault("final_action", "HOLD")
            state.setdefault("final_position_pct", 0.0)
            state.setdefault("final_reasoning", f"Graph error: {exc}")
            state.setdefault("execution_plan", {})

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
            # Structured agent run log for monitoring / debugging
            try:
                log_agent_run(ticker, {
                    "action": state.get("final_action", "HOLD"),
                    "position_pct": state.get("final_position_pct", 0.0),
                    "reasoning": (state.get("final_reasoning") or "")[:300],
                    "execution_ms": int(elapsed * 1000),
                    "status": "FAILED" if state.get("error") else "COMPLETED",
                    "macro": (state.get("macro_analyst_result") or {}).get("signal"),
                    "news": (state.get("news_sentiment_result") or {}).get("signal"),
                    "fundamentals": (state.get("fundamentals_result") or {}).get("signal"),
                    "technicals": (state.get("technicals_result") or {}).get("signal"),
                    "risk_approved": (state.get("risk_manager_result") or {}).get("approved"),
                })
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[graph] Failed to persist AgentRun for %s: %s", ticker, exc)
