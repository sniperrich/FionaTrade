from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.analysis.service import AnalysisService
from app.core.config import Settings
from app.db.models import Signal


@dataclass
class SignalRunResult:
    created: int
    skipped_low_confidence: int
    skipped_hold: int
    signal_ids: list[int]


class SignalEngineService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.analysis = AnalysisService(settings)

    def run(self, session: Session) -> SignalRunResult:
        events = self.analysis.pending_events(session)
        created = 0
        skipped_low_conf = 0
        skipped_hold = 0
        signal_ids: list[int] = []

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
            signal_ids=signal_ids,
        )
