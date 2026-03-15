from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.ingestion.service import IngestionService
from app.normalization.service import NormalizationService
from app.paper_engine.service import PaperEngineService
from app.signal_engine.service import SignalEngineService
from app.validation.service import ValidationService


class PipelineOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ingestion = IngestionService(settings)
        self.normalization = NormalizationService(settings)
        self.validation = ValidationService(settings.validation_corroboration_window_minutes)
        self.signal_engine = SignalEngineService(settings)
        self.paper_engine = PaperEngineService(settings)
        self._agent_graph = None  # lazy-init to avoid import cost when agent mode is off

    def _get_agent_graph(self):
        if self._agent_graph is None:
            from app.agent_graph.graph import AgentGraph
            self._agent_graph = AgentGraph(self.settings)
        return self._agent_graph

    def run_ingestion_validation(self, session: Session) -> dict:
        ingested = self.ingestion.run(session)
        clusters = self.normalization.build_clusters(session, raw_ids=ingested.raw_item_ids)
        validated = self.validation.validate_and_store(session, clusters)
        return {
            "ingestion": asdict(ingested),
            "normalization": {"clusters": len(clusters)},
            "validation": asdict(validated),
        }

    def run_signals(self, session: Session) -> dict:
        if self.settings.agent_mode_enabled:
            return self.run_agent_graph(session)
        result = self.signal_engine.run(session)
        return asdict(result)

    def run_agent_graph(self, session: Session) -> dict:
        """Run the multi-agent graph for all configured tickers."""
        tickers = list(
            self.settings.agent_tickers_override
            or getattr(self.settings, "sp100_tickers", [])
            or []
        )
        if not tickers:
            return {"agent_mode": True, "runs": [], "error": "No tickers configured"}

        graph = self._get_agent_graph()
        runs = []
        for ticker in tickers:
            state = graph.run(session, ticker)
            runs.append({
                "ticker": ticker,
                "action": state.get("final_action", "HOLD"),
                "position_pct": state.get("final_position_pct", 0.0),
                "reasoning": state.get("final_reasoning", ""),
                "error": state.get("error"),
            })
        return {"agent_mode": True, "runs": runs}

    def run_paper_execution(self, session: Session) -> dict:
        result = self.paper_engine.execute(session)
        return asdict(result)

    def portfolio(self, session: Session) -> dict:
        return self.paper_engine.portfolio(session)
