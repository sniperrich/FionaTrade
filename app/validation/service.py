from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.analysis.taxonomy import TIER_SCORE
from app.db.models import Event, EventEvidence
from app.normalization.service import NormalizedCluster


@dataclass
class ValidationResult:
    created_events: int
    valid_events: int
    watch_events: int
    rejected_events: int
    event_ids: list[int]


class ValidationService:
    def _has_conflict(self, cluster: NormalizedCluster) -> bool:
        text = " ".join(f"{i.title} {i.body}".lower() for i in cluster.raw_items)
        has_upgrade = "upgrade" in text or "beats" in text
        has_downgrade = "downgrade" in text or "miss" in text or "cuts" in text
        return has_upgrade and has_downgrade

    def _score(self, cluster: NormalizedCluster) -> tuple[int, str, str | None]:
        sources = {item.source for item in cluster.raw_items}
        tiers = [item.source_tier for item in cluster.raw_items]

        if not cluster.canonical.tickers:
            return 0, "REJECTED", "no_ticker_detected"

        source_count = len(sources)
        has_tier0 = any(t == 0 for t in tiers)
        conflict = self._has_conflict(cluster)

        source_score = max(TIER_SCORE.get(t, 10) for t in tiers)
        corroboration_score = 0 if source_count <= 1 else min(35, (source_count - 1) * 20)
        entity_consistency = 20 if len(cluster.canonical.tickers) == 1 else 12
        conflict_penalty = 40 if conflict else 0

        confidence = max(0, min(100, int(source_score + corroboration_score + entity_consistency - conflict_penalty)))

        min_tier = min(tiers)
        if conflict:
            return confidence, "WATCH", "source_conflict_detected"
        if has_tier0 or source_count >= 2 or min_tier <= 1:
            return confidence, "VALID", None
        return confidence, "WATCH", "single_source_only"

    def validate_and_store(self, session: Session, clusters: list[NormalizedCluster]) -> ValidationResult:
        created = 0
        valid = 0
        watch = 0
        rejected = 0
        event_ids: list[int] = []

        for cluster in clusters:
            confidence, status, reason = self._score(cluster)

            event = Event(
                event_type=cluster.canonical.event_type,
                entities=cluster.canonical.entities,
                tickers=cluster.canonical.tickers,
                severity=cluster.canonical.severity,
                event_time=cluster.canonical.event_time,
                confidence=confidence,
                validation_status=status,
                conflict_reason=reason,
                summary=cluster.canonical.summary,
            )
            session.add(event)
            session.flush()

            for raw in cluster.raw_items:
                session.add(
                    EventEvidence(
                        event_id=event.id,
                        raw_item_id=raw.id,
                        url=raw.url,
                        source=raw.source,
                        source_tier=raw.source_tier,
                        summary=raw.title[:280],
                    )
                )
                raw.processed = True

            created += 1
            event_ids.append(event.id)
            if status == "VALID":
                valid += 1
            elif status == "WATCH":
                watch += 1
            else:
                rejected += 1

        return ValidationResult(
            created_events=created,
            valid_events=valid,
            watch_events=watch,
            rejected_events=rejected,
            event_ids=event_ids,
        )
