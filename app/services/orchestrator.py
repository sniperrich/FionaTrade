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
        result = self.signal_engine.run(session)
        return asdict(result)

    def run_paper_execution(self, session: Session) -> dict:
        result = self.paper_engine.execute(session)
        return asdict(result)

    def portfolio(self, session: Session) -> dict:
        return self.paper_engine.portfolio(session)
