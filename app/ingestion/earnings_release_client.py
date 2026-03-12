from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, make_hash, utc_now
from app.db.models import EarningsCalendar
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem


class EarningsReleaseClient:
    _NY_TZ = ZoneInfo("America/New_York")

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _offline(reason: str, details: dict | None = None) -> tuple[list[RawNewsItem], SourceCheck]:
        return [], SourceCheck(
            source_key="earnings_release",
            source_name="earnings_release",
            source_type="structured",
            display_name="Structured Earnings Releases",
            status="OFFLINE",
            error_message=reason,
            details=details or {},
        )

    @staticmethod
    def _date_floor(raw: str | date) -> datetime:
        if isinstance(raw, date):
            day = raw
        else:
            day = date.fromisoformat(str(raw))
        return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("UTC"))

    @staticmethod
    def _surprise_pct(actual: float | None, estimate: float | None) -> float | None:
        if actual is None or estimate in (None, 0):
            return None
        return ((float(actual) - float(estimate)) / abs(float(estimate))) * 100.0

    @staticmethod
    def _surprise_label(pct: float | None) -> str:
        if pct is None:
            return "unknown"
        if pct >= 2.0:
            return "beat"
        if pct <= -2.0:
            return "miss"
        return "inline"

    @staticmethod
    def _format_number(value: float | None) -> str:
        if value is None:
            return "n/a"
        number = float(value)
        abs_number = abs(number)
        if abs_number >= 1_000_000_000:
            return f"{number / 1_000_000_000:.2f}B"
        if abs_number >= 1_000_000:
            return f"{number / 1_000_000:.2f}M"
        return f"{number:.2f}"

    def _release_timestamp(self, report_date: datetime, report_hour: str | None) -> datetime:
        release_date = ensure_utc(report_date).date()
        hour = str(report_hour or "").strip().lower()
        if hour in {"bmo", "before market open"} or "before market" in hour:
            local_time = dt_time(8, 0)
        elif hour in {"amc", "after market close"} or "after market" in hour:
            local_time = dt_time(16, 5)
        elif hour in {"dmh", "during market hours"} or "during market" in hour:
            local_time = dt_time(12, 0)
        else:
            local_time = dt_time(12, 0)
        return datetime.combine(release_date, local_time, tzinfo=self._NY_TZ).astimezone(ZoneInfo("UTC"))

    def _event_type_hint(self, eps_label: str, revenue_label: str) -> str:
        if eps_label == "miss" and revenue_label in {"miss", "inline", "unknown"}:
            return "earnings_miss"
        if eps_label in {"miss", "inline"} and revenue_label == "miss":
            return "earnings_miss"
        return "unknown"

    @staticmethod
    def _label_text(metric_name: str, label: str, surprise_pct: float | None) -> str:
        if label == "unknown":
            return f"{metric_name} estimate unavailable"
        if surprise_pct is None:
            return f"{metric_name} {label}"
        sign = "+" if surprise_pct >= 0 else ""
        return f"{metric_name} {label} {sign}{surprise_pct:.1f}%"

    def _build_item(self, row: EarningsCalendar) -> RawNewsItem:
        symbol = str(row.symbol or "").upper().strip()
        published_at = self._release_timestamp(row.report_date, row.report_hour)
        eps_surprise = self._surprise_pct(row.eps_actual, row.eps_estimate)
        revenue_surprise = self._surprise_pct(row.revenue_actual, row.revenue_estimate)
        eps_label = self._surprise_label(eps_surprise)
        revenue_label = self._surprise_label(revenue_surprise)
        event_type_hint = self._event_type_hint(eps_label, revenue_label)

        title = (
            f"{symbol} reports quarterly results: "
            f"{self._label_text('EPS', eps_label, eps_surprise)}, "
            f"{self._label_text('revenue', revenue_label, revenue_surprise)}"
        )
        body = (
            f"Structured earnings release for {symbol}. "
            f"Report date {ensure_utc(row.report_date).date().isoformat()} {str(row.report_hour or 'unspecified').upper()}. "
            f"Fiscal period Q{row.quarter if row.quarter is not None else '?'} {row.fiscal_year if row.fiscal_year is not None else 'unknown'}. "
            f"EPS actual {self._format_number(row.eps_actual)} versus estimate {self._format_number(row.eps_estimate)}. "
            f"EPS surprise {f'{eps_surprise:+.2f}%' if eps_surprise is not None else 'n/a'}. "
            f"Revenue actual {self._format_number(row.revenue_actual)} versus estimate {self._format_number(row.revenue_estimate)}. "
            f"Revenue surprise {f'{revenue_surprise:+.2f}%' if revenue_surprise is not None else 'n/a'}. "
            "This is a structured earnings-release event generated from local earnings calendar data."
        )
        quarter = row.quarter if row.quarter is not None else "na"
        fiscal_year = row.fiscal_year if row.fiscal_year is not None else "na"
        release_day = ensure_utc(row.report_date).date().isoformat()
        url = f"finnhub://earnings-release/{symbol}/{release_day}/q{quarter}/fy{fiscal_year}"
        metadata = {
            "ticker": symbol,
            "structured_ticker": True,
            "structured_source": "earnings_release",
            "origin_source": "finnhub_earnings_calendar",
            "report_date": release_day,
            "report_hour": row.report_hour,
            "quarter": row.quarter,
            "fiscal_year": row.fiscal_year,
            "eps_actual": row.eps_actual,
            "eps_estimate": row.eps_estimate,
            "eps_surprise_pct": round(eps_surprise, 2) if eps_surprise is not None else None,
            "revenue_actual": row.revenue_actual,
            "revenue_estimate": row.revenue_estimate,
            "revenue_surprise_pct": round(revenue_surprise, 2) if revenue_surprise is not None else None,
            "event_type_hint": event_type_hint,
        }
        return RawNewsItem(
            source="earnings_release",
            url=url,
            title=title,
            body=body,
            published_at=published_at,
            ingested_at=utc_now(),
            hash=make_hash("earnings_release", url, title),
            source_tier=0,
            metadata=metadata,
        )

    def build_from_calendar(
        self,
        session: Session,
        from_date: str,
        to_date: str,
        *,
        symbols: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> tuple[list[RawNewsItem], SourceCheck]:
        if not self.settings.enable_earnings_release_source:
            return self._offline("Earnings release source disabled by config")

        allowed = {ticker.upper() for ticker in self.settings.sp100_tickers}
        if symbols:
            allowed &= {ticker.upper() for ticker in symbols}

        start_dt = self._date_floor(from_date)
        end_dt = self._date_floor(to_date) + timedelta(days=1)
        rows = (
            session.execute(
                select(EarningsCalendar)
                .where(
                    and_(
                        EarningsCalendar.symbol.in_(sorted(allowed)),
                        EarningsCalendar.report_date >= start_dt,
                        EarningsCalendar.report_date < end_dt,
                    )
                )
                .order_by(EarningsCalendar.report_date.asc(), EarningsCalendar.symbol.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            existing = session.execute(select(EarningsCalendar.id).limit(1)).first()
            if existing is None:
                return self._offline(
                    "earnings_calendar table is empty; run backfill_earnings_calendar.py first",
                    details={"from": from_date, "to": to_date},
                )
            return [], SourceCheck(
                source_key="earnings_release",
                source_name="earnings_release",
                source_type="structured",
                display_name="Structured Earnings Releases",
                status="ONLINE",
                details={"items": 0, "calendar_rows": 0, "from": from_date, "to": to_date},
            )

        as_of_utc = ensure_utc(as_of) if as_of is not None else None
        items: list[RawNewsItem] = []
        skipped_future = 0
        for row in rows:
            published_at = self._release_timestamp(row.report_date, row.report_hour)
            if as_of_utc is not None and published_at > as_of_utc:
                skipped_future += 1
                continue
            items.append(self._build_item(row))

        return items, SourceCheck(
            source_key="earnings_release",
            source_name="earnings_release",
            source_type="structured",
            display_name="Structured Earnings Releases",
            status="ONLINE",
            details={
                "items": len(items),
                "calendar_rows": len(rows),
                "skipped_future": skipped_future,
                "from": from_date,
                "to": to_date,
            },
        )

    def fetch_recent(self, session: Session, as_of: datetime | None = None) -> tuple[list[RawNewsItem], SourceCheck]:
        current = ensure_utc(as_of or utc_now())
        lookback_days = max(1, int(self.settings.earnings_release_ingestion_lookback_days))
        from_date = (current.date() - timedelta(days=lookback_days)).isoformat()
        to_date = current.date().isoformat()
        return self.build_from_calendar(session, from_date=from_date, to_date=to_date, as_of=current)
