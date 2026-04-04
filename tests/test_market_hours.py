from __future__ import annotations

from datetime import datetime, timezone

from app.core.market_hours import is_holiday


def test_is_holiday_handles_2027_new_years_day() -> None:
    assert is_holiday(datetime(2027, 1, 1, 15, 0, tzinfo=timezone.utc)) is True


def test_is_holiday_handles_2027_thanksgiving() -> None:
    assert is_holiday(datetime(2027, 11, 25, 15, 0, tzinfo=timezone.utc)) is True


def test_is_holiday_rejects_normal_2027_trading_day() -> None:
    assert is_holiday(datetime(2027, 11, 24, 15, 0, tzinfo=timezone.utc)) is False
