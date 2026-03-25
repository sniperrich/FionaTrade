from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import IngestionCursor
from app.ingestion.sec_client import SecClient


def test_sec_timestamp_prefers_acceptance_datetime(settings):
    client = SecClient(settings)
    published = client._parse_published_at("2026-01-02T13:39:53-05:00", "2026-01-02")
    assert published.year == 2026
    assert published.month == 1
    assert published.day == 2
    assert published.hour == 13
    assert published.minute == 39


def test_sec_timestamp_falls_back_to_filing_date(settings):
    client = SecClient(settings)
    published = client._parse_published_at("not-a-time", "2026-01-02")
    assert published.year == 2026
    assert published.month == 1
    assert published.day == 2


def test_sec_timestamp_uses_now_when_all_invalid(settings):
    client = SecClient(settings)
    published = client._parse_published_at(None, "invalid-date")
    assert isinstance(published, datetime)


def test_sec_fetch_uses_accession_cursor_for_incremental(session, settings):
    settings.enable_sec = True
    settings.sec_recent_max_age_days = 30
    client = SecClient(settings)

    now = datetime.now(timezone.utc)
    session.add(IngestionCursor(cursor_key="sec_last_accession:AAPL", cursor_value="0000000000-24-000002"))
    session.flush()

    def _ticker_map():
        return {"AAPL": "0000320193"}

    def _request_json_with_retry(*_args, **_kwargs):
        payload = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "10-Q", "10-Q"],
                    "filingDate": [
                        now.date().isoformat(),
                        now.date().isoformat(),
                        now.date().isoformat(),
                    ],
                    "acceptanceDateTime": [
                        (now - timedelta(minutes=1)).isoformat(),
                        (now - timedelta(minutes=2)).isoformat(),
                        (now - timedelta(minutes=3)).isoformat(),
                    ],
                    "accessionNumber": [
                        "0000000000-24-000004",
                        "0000000000-24-000003",
                        "0000000000-24-000002",
                    ],
                    "primaryDocument": ["a.htm", "b.htm", "c.htm"],
                }
            }
        }
        return payload, 200, None

    client._ticker_cik_map = _ticker_map  # type: ignore[method-assign]
    client._request_json_with_retry = _request_json_with_retry  # type: ignore[method-assign]

    items, check = client.fetch(session, tickers=["AAPL"])
    session.flush()

    accessions = sorted(str(item.metadata.get("accession")) for item in items)
    assert accessions == ["0000000000-24-000003", "0000000000-24-000004"]
    assert check.status == "ONLINE"

    cursor = session.execute(
        select(IngestionCursor).where(IngestionCursor.cursor_key == "sec_last_accession:AAPL")
    ).scalar_one()
    assert cursor.cursor_value == "0000000000-24-000004"


def test_sec_fetch_skips_old_filings_and_bootstraps_cursor(session, settings):
    settings.enable_sec = True
    settings.sec_recent_max_age_days = 2
    client = SecClient(settings)

    now = datetime.now(timezone.utc)
    very_old = now - timedelta(days=20)

    def _ticker_map():
        return {"AAPL": "0000320193"}

    def _request_json_with_retry(*_args, **_kwargs):
        payload = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "10-K"],
                    "filingDate": [very_old.date().isoformat(), very_old.date().isoformat()],
                    "acceptanceDateTime": [very_old.isoformat(), (very_old - timedelta(hours=1)).isoformat()],
                    "accessionNumber": ["0000000000-24-001111", "0000000000-24-001110"],
                    "primaryDocument": ["a.htm", "b.htm"],
                }
            }
        }
        return payload, 200, None

    client._ticker_cik_map = _ticker_map  # type: ignore[method-assign]
    client._request_json_with_retry = _request_json_with_retry  # type: ignore[method-assign]

    items, check = client.fetch(session, tickers=["AAPL"])
    session.flush()

    assert items == []
    assert check.status == "ONLINE"
    assert (check.details or {}).get("skipped_old_filings") == 2

    cursor = session.execute(
        select(IngestionCursor).where(IngestionCursor.cursor_key == "sec_last_accession:AAPL")
    ).scalar_one()
    assert cursor.cursor_value == "0000000000-24-001111"
