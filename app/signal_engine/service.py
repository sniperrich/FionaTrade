from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.analysis.service import AnalysisService
from app.analysis.signal_validator import (
    ExecutionRecommendation,
    SignalValidator,
)
from app.core.config import Settings
from app.core.logging import log_writeout
from app.db.models import Signal

logger = logging.getLogger(__name__)


@dataclass
class SignalRunResult:
    created: int
    skipped_low_confidence: int
    skipped_hold: int
    skipped_validation: int
    signal_ids: list[int]


class SignalEngineService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.analysis = AnalysisService(settings)
        self.validator = SignalValidator(settings)

    def run(self, session: Session) -> SignalRunResult:
        events = self.analysis.pending_events(session)
        created = 0
        skipped_low_conf = 0
        skipped_hold = 0
        skipped_validation = 0
        signal_ids: list[int] = []

        validation_enabled = getattr(self.settings, "validation_enabled", True)
        min_review_score = getattr(self.settings, "validation_min_review_score", 40)
        allow_downweight = getattr(self.settings, "validation_allow_downweight_execution", True)

        for event in events:
            if event.confidence < self.settings.min_trade_confidence:
                event.signaled = True
                skipped_low_conf += 1
                continue

            signal = self.analysis.event_to_signal(event, session=session)
            if not signal:
                event.signaled = True
                skipped_hold += 1
                continue

            if signal.action == "HOLD":
                event.signaled = True
                skipped_hold += 1
                continue

            # ── Signal Validation Gate ────────────────────────────────────────
            if validation_enabled:
                vr = self.validator.validate(event=event, signal=signal)
                blocked = (
                    vr.execution_recommendation
                    in (ExecutionRecommendation.REJECT, ExecutionRecommendation.NO_TRADE)
                    or vr.review_score < min_review_score
                    or (
                        vr.execution_recommendation == ExecutionRecommendation.DOWNWEIGHT
                        and not allow_downweight
                    )
                )
                if blocked:
                    log_writeout(
                        "signal_validation_blocked",
                        {
                            "ticker": signal.ticker,
                            "event_type": event.event_type,
                            "recommendation": vr.execution_recommendation.value,
                            "review_score": vr.review_score,
                            "tags": vr.issue_tags,
                        },
                    )
                    event.signaled = True
                    skipped_validation += 1
                    continue
            # ─────────────────────────────────────────────────────────────────

            row = Signal(
                event_id=event.id,
                action=signal.action,
                ticker=signal.ticker,
                confidence=signal.confidence,
                horizon_min=signal.horizon_min,
                reason=signal.reason,
                expires_at=signal.expires_at,
                fallback_used=signal.fallback_used,
                status="ACTIVE",
            )
            session.add(row)
            session.flush()
            signal_ids.append(row.id)
            created += 1
            event.signaled = True

        return SignalRunResult(
            created=created,
            skipped_low_confidence=skipped_low_conf,
            skipped_hold=skipped_hold,
            skipped_validation=skipped_validation,
            signal_ids=signal_ids,
        )
