from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dateutil import parser as dt_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import EarningsCalendar, SourceStatus
from app.ingestion.finnhub_client import FinnhubNewsClient


@dataclass
class EarningsCalendarRefreshResult:
    fetched: int
    upserted: int
    skipped: int
    from_date: str
    to_date: str


class EarningsCalendarService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.finnhub = FinnhubNewsClient(settings)

    def _persist_source_status(self, session: Session, status: str, error_message: str | None, details: dict) -> None:
        now = utc_now()
        row = session.execute(
            select(SourceStatus).where(SourceStatus.source_key == "finnhub_earnings_calendar")
        ).scalar_one_or_none()
        if row is None:
            row = SourceStatus(
                source_key="finnhub_earnings_calendar",
                source_name="finnhub",
                source_type="finnhub",
                display_name="Finnhub Earnings Calendar",
            )
            session.add(row)
        row.status = status
        row.error_message = error_message if status == "OFFLINE" else None
        row.details_json = details or {}
        row.last_checked_at = now
        if status == "ONLINE":
            row.last_success_at = now

    @staticmethod
    def _parse_report_date(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            parsed = dt_parser.parse(raw)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def refresh(
        self,
        session: Session,
        from_date: str,
        to_date: str,
        symbols: list[str] | None = None,
    ) -> EarningsCalendarRefreshResult:
        rows, check = self.finnhub.fetch_earnings_calendar(from_date=from_date, to_date=to_date, symbols=symbols)
        self._persist_source_status(session, check.status, check.error_message, check.details)

        upserted = 0
        skipped = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").upper().strip()
            report_date = self._parse_report_date(row.get("date"))
            quarter = self._to_int(row.get("quarter"))
            fiscal_year = self._to_int(row.get("year"))
            if not symbol or report_date is None:
                skipped += 1
                continue

            existing = session.execute(
                select(EarningsCalendar).where(
                    EarningsCalendar.symbol == symbol,
                    EarningsCalendar.report_date == report_date,
                    EarningsCalendar.quarter == quarter,
                    EarningsCalendar.fiscal_year == fiscal_year,
                )
            ).scalar_one_or_none()

            if existing is None:
                existing = EarningsCalendar(
                    symbol=symbol,
                    report_date=report_date,
                    quarter=quarter,
                    fiscal_year=fiscal_year,
                )
                session.add(existing)

            existing.report_hour = str(row.get("hour") or "").strip() or None
            existing.eps_actual = self._to_float(row.get("epsActual"))
            existing.eps_estimate = self._to_float(row.get("epsEstimate"))
            existing.revenue_actual = self._to_float(row.get("revenueActual"))
            existing.revenue_estimate = self._to_float(row.get("revenueEstimate"))
            existing.source = "finnhub"
            existing.fetched_at = utc_now()
            upserted += 1

        return EarningsCalendarRefreshResult(
            fetched=len(rows),
            upserted=upserted,
            skipped=skipped,
            from_date=from_date,
            to_date=to_date,
        )

    def refresh_if_due(self, session: Session, now: datetime | None = None) -> EarningsCalendarRefreshResult | None:
        if not self.settings.earnings_calendar_auto_refresh:
            return None
        current = ensure_utc(now or utc_now())
        latest = session.execute(
            select(EarningsCalendar).order_by(EarningsCalendar.fetched_at.desc()).limit(1)
        ).scalar_one_or_none()
        if latest and latest.fetched_at:
            age = current - ensure_utc(latest.fetched_at)
            if age < timedelta(hours=max(1, int(self.settings.earnings_calendar_refresh_interval_hours))):
                return None

        from_date = (current.date() - timedelta(days=max(1, int(self.settings.earnings_calendar_lookback_days)))).isoformat()
        to_date = (current.date() + timedelta(days=max(1, int(self.settings.earnings_calendar_lookahead_days)))).isoformat()
        return self.refresh(session, from_date=from_date, to_date=to_date)

