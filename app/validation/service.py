from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import TIER_SCORE, resolve_event_type_for_text
from app.core.utils import ensure_utc
from app.db.models import Event, EventEvidence
from app.normalization.service import NormalizedCluster


@dataclass
class ValidationResult:
    created_events: int
    valid_events: int
    watch_events: int
    rejected_events: int
    event_ids: list[int]


@dataclass
class _RecentEventSnapshot:
    event_time: object
    tickers: list[str]
    event_type: str
    summary: str
    sources: set[str]
    tiers: list[int]


class ValidationService:
    def __init__(self, corroboration_window_minutes: int = 180):
        self.corroboration_window = timedelta(minutes=max(1, int(corroboration_window_minutes)))

    @staticmethod
    def _normalize_source_tier(raw_tier: object) -> int:
        if raw_tier is None:
            return 9
        try:
            return int(raw_tier)
        except (TypeError, ValueError):
            return 9

    @staticmethod
    def _event_family(event_type: str, summary: str) -> str:
        effective = resolve_event_type_for_text(event_type, summary)
        if effective in {"earnings_miss", "guidance_cut", "sec_earnings_release"}:
            return "earnings_window"
        return effective

    @staticmethod
    def _current_text(cluster: NormalizedCluster) -> str:
        return " ".join(f"{item.title} {item.body}".strip() for item in cluster.raw_items if item.title or item.body).lower()

    @staticmethod
    def _summaries_overlap(a: str, b: str, min_common_words: int = 4) -> bool:
        stopwords = {
            "the", "a", "an", "is", "in", "of", "to", "and", "for", "on",
            "at", "as", "by", "with", "its", "it", "be", "has", "have",
        }
        words_a = {w for w in a.lower().split() if len(w) >= 4 and w not in stopwords}
        words_b = {w for w in b.lower().split() if len(w) >= 4 and w not in stopwords}
        return len(words_a & words_b) >= min_common_words

    def _has_conflict(self, texts: list[str]) -> bool:
        combined = " ".join(t.lower() for t in texts if t)
        has_upgrade = any(token in combined for token in ("upgrade", "beats", "guides above", "wins case", "favorable ruling"))
        has_downgrade = any(
            token in combined
            for token in ("downgrade", "miss", "cuts", "fraud", "penalty", "lawsuit filed", "investigation")
        )
        return has_upgrade and has_downgrade

    def _corroborates(self, cluster: NormalizedCluster, snapshot: _RecentEventSnapshot) -> bool:
        current_tickers = {str(t).upper() for t in (cluster.canonical.tickers or [])}
        prior_tickers = {str(t).upper() for t in (snapshot.tickers or [])}
        if not current_tickers or not prior_tickers or current_tickers.isdisjoint(prior_tickers):
            return False

        current_summary = cluster.canonical.summary or self._current_text(cluster)
        current_family = self._event_family(cluster.canonical.event_type, current_summary)
        prior_family = self._event_family(snapshot.event_type, snapshot.summary)

        if current_family == prior_family and current_family != "unknown":
            return True
        return self._summaries_overlap(current_summary, snapshot.summary)

    def _load_recent_history(self, session: Session, clusters: list[NormalizedCluster]) -> list[_RecentEventSnapshot]:
        if not clusters:
            return []
        min_event_time = min(ensure_utc(cluster.canonical.event_time) for cluster in clusters)
        history_cutoff = min_event_time - self.corroboration_window
        events = session.execute(
            select(Event).where(Event.event_time >= history_cutoff).order_by(Event.event_time.asc())
        ).scalars().all()
        if not events:
            return []

        event_ids = [event.id for event in events if event.id is not None]
        evidence_rows = session.execute(
            select(EventEvidence.event_id, EventEvidence.source, EventEvidence.source_tier).where(
                EventEvidence.event_id.in_(event_ids)
            )
        ).all()
        by_event_id: dict[int, list[tuple[str, int]]] = {}
        for event_id, source, tier in evidence_rows:
            by_event_id.setdefault(int(event_id), []).append((str(source or "").lower(), int(tier or 9)))

        history: list[_RecentEventSnapshot] = []
        for event in events:
            evidence = by_event_id.get(int(event.id or 0), [])
            history.append(
                _RecentEventSnapshot(
                    event_time=ensure_utc(event.event_time),
                    tickers=[str(t).upper() for t in (event.tickers or [])],
                    event_type=event.event_type,
                    summary=event.summary or "",
                    sources={source for source, _ in evidence},
                    tiers=[tier for _, tier in evidence] or [2],
                )
            )
        return history

    def _score(
        self,
        cluster: NormalizedCluster,
        recent_history: list[_RecentEventSnapshot],
    ) -> tuple[int, str, str | None]:
        if not cluster.canonical.tickers:
            return 0, "REJECTED", "no_ticker_detected"

        event_time = ensure_utc(cluster.canonical.event_time)
        summary = cluster.canonical.summary or self._current_text(cluster)
        sources = {str(item.source or "").lower() for item in cluster.raw_items if item.source}
        tiers = [self._normalize_source_tier(item.source_tier) for item in cluster.raw_items]
        conflict_texts = [summary]

        for snapshot in recent_history:
            snap_time = ensure_utc(snapshot.event_time)
            if snap_time > event_time:
                continue
            if event_time - snap_time > self.corroboration_window:
                continue
            if not self._corroborates(cluster, snapshot):
                continue
            sources.update(snapshot.sources)
            tiers.extend(snapshot.tiers)
            conflict_texts.append(snapshot.summary)

        source_count = len(sources)
        has_tier0 = any(t == 0 for t in tiers)
        conflict = self._has_conflict(conflict_texts)

        source_score = max(TIER_SCORE.get(t, 10) for t in tiers)
        corroboration_score = 0 if source_count <= 1 else min(35, (source_count - 1) * 20)
        entity_consistency = 20 if len(cluster.canonical.tickers) == 1 else 12
        conflict_penalty = 40 if conflict else 0

        confidence = max(0, min(100, int(source_score + corroboration_score + entity_consistency - conflict_penalty)))
        if conflict:
            return confidence, "WATCH", "source_conflict_detected"
        if has_tier0 or source_count >= 2:
            return confidence, "VALID", None
        return confidence, "WATCH", "single_source_only"

    def _snapshot_from_cluster(self, cluster: NormalizedCluster) -> _RecentEventSnapshot:
        return _RecentEventSnapshot(
            event_time=ensure_utc(cluster.canonical.event_time),
            tickers=[str(t).upper() for t in (cluster.canonical.tickers or [])],
            event_type=cluster.canonical.event_type,
            summary=cluster.canonical.summary or "",
            sources={str(item.source or "").lower() for item in cluster.raw_items if item.source},
            tiers=[self._normalize_source_tier(item.source_tier) for item in cluster.raw_items] or [2],
        )

    def validate_and_store(self, session: Session, clusters: list[NormalizedCluster]) -> ValidationResult:
        created = 0
        valid = 0
        watch = 0
        rejected = 0
        event_ids: list[int] = []

        ordered_clusters = sorted(clusters, key=lambda cluster: ensure_utc(cluster.canonical.event_time))
        historical_snapshots = self._load_recent_history(session, ordered_clusters)
        batch_snapshots: list[_RecentEventSnapshot] = []

        for cluster in ordered_clusters:
            confidence, status, reason = self._score(cluster, recent_history=[*historical_snapshots, *batch_snapshots])

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

            batch_snapshots.append(self._snapshot_from_cluster(cluster))
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
