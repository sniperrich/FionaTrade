from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, normalize_title, utc_now
from app.db.models import RawItem, SourceStatus
from app.ingestion.earnings_release_client import EarningsReleaseClient
from app.ingestion.finnhub_client import FinnhubNewsClient, FinnhubClient
from app.ingestion.fred_client import FREDClient
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
        self.fred = FREDClient(settings)
        self.fundamentals = FinnhubClient(settings)

    def _recent_titles(self, session: Session) -> set[str]:
        cutoff = utc_now() - timedelta(hours=6)
        rows = session.execute(select(RawItem.title).where(RawItem.ingested_at >= cutoff)).all()
        return {normalize_title(r[0]) for r in rows}

    def _live_tickers(self, requested: list[str] | None = None) -> list[str]:
        if requested:
            return [str(t).upper().strip() for t in requested if str(t).strip()]
        preferred = list(self.settings.live_trading_tickers or list(self.settings.agent_tickers_override or []))
        if preferred:
            return [str(t).upper().strip() for t in preferred if str(t).strip()]
        max_tickers = self.settings.ticker_rss_max_tickers
        if max_tickers > 0:
            return list(self.settings.sp100_tickers[:max_tickers])
        return list(self.settings.sp100_tickers)

    def _collect(
        self,
        session: Session,
        *,
        profile: str = "full",
        tickers: list[str] | None = None,
    ) -> tuple[list[RawNewsItem], list[SourceCheck]]:
        items: list[RawNewsItem] = []
        checks: list[SourceCheck] = []
        fast_profile = profile in {"fast", "live_fast", "scheduled_fast"}

        if not fast_profile:
            sec_items, sec_check = self.sec.fetch(session)
            items.extend(sec_items)
            checks.append(sec_check)

        rss_items, rss_checks = self.rss.fetch()
        items.extend(rss_items)
        checks.extend(rss_checks)

        # Per-ticker Yahoo Finance RSS (ticker-specific headlines, tier 1)
        if fast_profile:
            tickers_to_fetch = self._live_tickers(tickers)
        else:
            max_tickers = self.settings.ticker_rss_max_tickers
            tickers_to_fetch = (
                self.settings.sp100_tickers[:max_tickers]
                if max_tickers > 0
                else self.settings.sp100_tickers
            )
        ticker_rss_items, ticker_rss_checks = self.rss.fetch_ticker_news(tickers_to_fetch)
        items.extend(ticker_rss_items)
        checks.extend(ticker_rss_checks)

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
        merged_checks: dict[str, SourceCheck] = {}
        grouped: dict[str, list[SourceCheck]] = defaultdict(list)
        for check in checks:
            grouped[check.source_key].append(check)

        for source_key, group in grouped.items():
            first = group[0]
            online_checks = [check for check in group if check.status == "ONLINE"]
            offline_checks = [check for check in group if check.status == "OFFLINE"]
            merged_details = {
                "checks": [
                    {
                        "status": check.status,
                        "display_name": check.display_name,
                        "error_message": check.error_message,
                        "details": check.details or {},
                    }
                    for check in group
                ],
                "online_count": len(online_checks),
                "offline_count": len(offline_checks),
            }
            if online_checks:
                winner = online_checks[0]
                status = "ONLINE"
                error_message = None
                merged_details["summary"] = "at least one feed on this source is healthy"
            else:
                winner = first
                status = "OFFLINE"
                errors = [check.error_message for check in offline_checks if check.error_message]
                error_message = " | ".join(dict.fromkeys(errors))[:4000] if errors else None
                merged_details["summary"] = "all feeds on this source are failing"

            merged_checks[source_key] = SourceCheck(
                source_key=source_key,
                source_name=winner.source_name,
                source_type=winner.source_type,
                display_name=winner.display_name,
                status=status,
                error_message=error_message,
                details=merged_details,
            )

        for check in merged_checks.values():
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

    def run(
        self,
        session: Session,
        *,
        profile: str = "full",
        tickers: list[str] | None = None,
    ) -> IngestionResult:
        fetched_items, checks = self._collect(session, profile=profile, tickers=tickers)
        return self.persist_items(session, fetched_items, checks)

    def refresh_macro_indicators(self, session: Session) -> dict:
        """Fetch latest FRED macro indicators and upsert into MacroIndicator table."""
        from app.core.logging import get_app_logger
        logger = get_app_logger()
        try:
            result = self.fred.upsert_indicators(session)
            if result.get("skipped"):
                logger.info("[ingestion] FRED refresh skipped: no API key configured")
            else:
                logger.info(
                    "[ingestion] FRED refresh: fetched=%d upserted=%d series=%s",
                    result.get("fetched", 0),
                    result.get("upserted", 0),
                    result.get("series", []),
                )
            return result
        except Exception as exc:
            logger.warning("[ingestion] FRED refresh failed: %s", exc)
            return {"error": str(exc)}

    def refresh_fundamentals_batch(self, session: Session, tickers: list[str] | None = None) -> dict:
        """Refresh fundamentals snapshots and analyst ratings for a list of tickers."""
        from app.core.logging import get_app_logger
        logger = get_app_logger()
        if tickers is None:
            tickers = list(self.settings.agent_tickers_override or self.settings.sp100_tickers or [])
        if not tickers:
            return {"tickers_updated": 0, "tickers_failed": 0, "skipped": True}
        try:
            result = self.fundamentals.refresh_fundamentals_batch(session, tickers)
            logger.info("[ingestion] Fundamentals batch: %s", result)
            return result
        except Exception as exc:
            logger.warning("[ingestion] Fundamentals batch failed: %s", exc)
            return {"error": str(exc)}
