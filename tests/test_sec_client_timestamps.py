from __future__ import annotations

from datetime import datetime

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
