from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, normalize_title, utc_now
from app.db.models import RawItem, SourceStatus
from app.ingestion.earnings_release_client import EarningsReleaseClient
from app.ingestion.finnhub_client import FinnhubNewsClient
from app.ingestion.rss_client import RssClient
from app.ingestion.sec_client import SecClient
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem


@dataclass
class IngestionResult:
    fetched: int
    inserted: int
    duplicate_dropped: int
    raw_item_ids: list[int]


class IngestionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sec = SecClient(settings)
        self.rss = RssClient(settings)
        self.finnhub = FinnhubNewsClient(settings)
        self.earnings_release = EarningsReleaseClient(settings)

    def _recent_titles(self, session: Session) -> set[str]:
        cutoff = utc_now() - timedelta(hours=6)
        rows = session.execute(select(RawItem.title).where(RawItem.ingested_at >= cutoff)).all()
        return {normalize_title(r[0]) for r in rows}

    def _collect(self, session: Session) -> tuple[list[RawNewsItem], list[SourceCheck]]:
        items: list[RawNewsItem] = []
        checks: list[SourceCheck] = []

        sec_items, sec_check = self.sec.fetch(session)
        items.extend(sec_items)
        checks.append(sec_check)

        rss_items, rss_checks = self.rss.fetch()
        items.extend(rss_items)
        checks.extend(rss_checks)

        finnhub_items, finnhub_check = self.finnhub.fetch()
        items.extend(finnhub_items)
        checks.append(finnhub_check)

        earnings_items, earnings_check = self.earnings_release.fetch_recent(session)
        items.extend(earnings_items)
        checks.append(earnings_check)

        return items, checks

    def _exists(self, session: Session, item: RawNewsItem) -> bool:
        stmt = select(RawItem.id).where(or_(RawItem.url == item.url, RawItem.item_hash == item.hash)).limit(1)
        return session.execute(stmt).first() is not None

    def _persist_source_checks(self, session: Session, checks: list[SourceCheck]) -> None:
        now = utc_now()
        for check in checks:
            row = session.execute(
                select(SourceStatus).where(SourceStatus.source_key == check.source_key)
            ).scalar_one_or_none()

            if not row:
                row = SourceStatus(
                    source_key=check.source_key,
                    source_name=check.source_name,
                    source_type=check.source_type,
                    display_name=check.display_name,
                )
                session.add(row)

            row.source_name = check.source_name
            row.source_type = check.source_type
            row.display_name = check.display_name
            row.status = check.status
            row.error_message = check.error_message if check.status == "OFFLINE" else None
            row.details_json = check.details or {}
            row.last_checked_at = now
            if check.status == "ONLINE":
                row.last_success_at = now

    def persist_items(self, session: Session, fetched_items: list[RawNewsItem], checks: list[SourceCheck]) -> IngestionResult:
        self._persist_source_checks(session, checks)

        recent_title_set = self._recent_titles(session)
        inserted = 0
        duplicates = 0
        raw_ids: list[int] = []

        for item in fetched_items:
            normalized_title = normalize_title(item.title)
            if normalized_title in recent_title_set:
                duplicates += 1
                continue
            if self._exists(session, item):
                duplicates += 1
                continue

            row = RawItem(
                source=item.source,
                source_tier=item.source_tier,
                url=item.url,
                title=item.title,
                body=item.body,
                published_at=ensure_utc(item.published_at),
                ingested_at=ensure_utc(item.ingested_at),
                item_hash=item.hash,
                metadata_json={**item.metadata, "normalized_title": normalized_title},
                processed=False,
            )
            session.add(row)
            session.flush()

            inserted += 1
            raw_ids.append(row.id)
            recent_title_set.add(normalized_title)

        return IngestionResult(
            fetched=len(fetched_items),
            inserted=inserted,
            duplicate_dropped=duplicates,
            raw_item_ids=raw_ids,
        )

    def run(self, session: Session) -> IngestionResult:
        fetched_items, checks = self._collect(session)
        return self.persist_items(session, fetched_items, checks)
