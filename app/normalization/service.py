from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import EVENT_KEYWORDS
from app.core.config import Settings
from app.core.utils import minute_bucket
from app.db.models import RawItem
from app.schemas.types import CanonicalEvent


@dataclass
class NormalizedCluster:
    canonical: CanonicalEvent
    raw_items: list[RawItem]


class NormalizationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.universe = set(settings.sp100_tickers)

    def _extract_tickers(self, text: str, metadata_json: dict) -> list[str]:
        found = []
        tokens = text.replace("$", " ").replace(",", " ").replace(".", ". ").split()
        for token in tokens:
            cleaned = token.strip().upper()
            if cleaned in self.universe and cleaned not in found:
                found.append(cleaned)

        hint = (metadata_json or {}).get("ticker")
        if hint and hint in self.universe and hint not in found:
            found.append(hint)
        return found

    def _infer_event_type(self, text: str) -> str:
        lowered = text.lower()
        for event_type, keywords in EVENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in lowered:
                    return event_type
        return "policy_shock"

    def _severity(self, event_type: str) -> int:
        severe = {"financial_fraud", "audit_issue", "regulatory_penalty", "accident_disaster"}
        mid = {"earnings_miss", "guidance_cut", "major_litigation", "supply_chain_disruption"}
        if event_type in severe:
            return 85
        if event_type in mid:
            return 70
        return 55

    def build_clusters(self, session: Session, raw_ids: Iterable[int] | None = None) -> list[NormalizedCluster]:
        stmt = select(RawItem).where(RawItem.processed.is_(False))
        if raw_ids:
            stmt = stmt.where(RawItem.id.in_(list(raw_ids)))
        rows = session.execute(stmt.order_by(RawItem.published_at.asc())).scalars().all()

        grouped: dict[tuple[str, str, str], NormalizedCluster] = {}
        for item in rows:
            text = f"{item.title} {item.body}"
            tickers = self._extract_tickers(text, item.metadata_json)
            event_type = self._infer_event_type(text)
            primary_ticker = tickers[0] if tickers else "UNKNOWN"
            bucket = minute_bucket(item.published_at, width_min=30).isoformat()
            key = (primary_ticker, event_type, bucket)

            if key not in grouped:
                grouped[key] = NormalizedCluster(
                    canonical=CanonicalEvent(
                        event_type=event_type,
                        entities=tickers,
                        tickers=tickers,
                        severity=self._severity(event_type),
                        event_time=item.published_at,
                        evidence_refs=[item.id],
                        summary=item.title[:280],
                    ),
                    raw_items=[item],
                )
            else:
                group = grouped[key]
                group.raw_items.append(item)
                group.canonical.evidence_refs.append(item.id)
                for t in tickers:
                    if t not in group.canonical.tickers:
                        group.canonical.tickers.append(t)
                        group.canonical.entities.append(t)
                if item.published_at < group.canonical.event_time:
                    group.canonical.event_time = item.published_at

        return list(grouped.values())
