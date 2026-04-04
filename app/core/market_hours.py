"""US market hours utilities — all times in US/Eastern (ET).

All functions are pure (no I/O) so they are safe to call anywhere.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Regular session: 09:30 – 16:00 ET
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# Extended hours (pre-market / after-hours)
_PRE_MARKET_START = time(4, 0)
_AFTER_HOURS_END = time(20, 0)

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(weeks=n - 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _observed_date(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def _nyse_holidays(year: int) -> set[tuple[int, int, int]]:
    holidays = {
        _observed_date(date(year, 1, 1)),               # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),          # MLK Day
        _nth_weekday_of_month(year, 2, 0, 3),          # Presidents Day
        _easter_sunday(year) - timedelta(days=2),      # Good Friday
        _last_weekday_of_month(year, 5, 0),            # Memorial Day
        _observed_date(date(year, 6, 19)),             # Juneteenth
        _observed_date(date(year, 7, 4)),              # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),          # Labor Day
        _nth_weekday_of_month(year, 11, 3, 4),         # Thanksgiving
        _observed_date(date(year, 12, 25)),            # Christmas
    }
    return {(d.year, d.month, d.day) for d in holidays}


def et_now() -> datetime:
    """Return current time in US Eastern timezone."""
    return datetime.now(_ET)


def is_holiday(dt: datetime | None = None) -> bool:
    """Return True if the date is a NYSE holiday."""
    d = (dt or et_now()).astimezone(_ET)
    return (d.year, d.month, d.day) in _nyse_holidays(d.year)


def is_market_open(dt: datetime | None = None) -> bool:
    """Return True if regular trading session is active (09:30–16:00 ET, Mon–Fri)."""
    et = (dt or et_now()).astimezone(_ET)
    if et.weekday() >= 5:        # Saturday=5, Sunday=6
        return False
    if is_holiday(et):
        return False
    t = et.time()
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def market_session_label(dt: datetime | None = None) -> str:
    """Return the current trading session label.

    Returns one of:
        'market_open'    — regular session 09:30–16:00
        'pre_market'     — 04:00–09:30
        'after_hours'    — 16:00–20:00
        'closed'         — overnight / weekend / holiday
    """
    et = (dt or et_now()).astimezone(_ET)
    if et.weekday() >= 5 or is_holiday(et):
        return "closed"
    t = et.time()
    if _MARKET_OPEN <= t < _MARKET_CLOSE:
        return "market_open"
    if _PRE_MARKET_START <= t < _MARKET_OPEN:
        return "pre_market"
    if _MARKET_CLOSE <= t < _AFTER_HOURS_END:
        return "after_hours"
    return "closed"


def minutes_until_open(dt: datetime | None = None) -> int | None:
    """Return minutes until next regular session open. None if market is open."""
    et = (dt or et_now()).astimezone(_ET)
    if is_market_open(et):
        return None  # already open

    # Find next trading day 09:30 ET
    candidate = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if et.time() >= _MARKET_OPEN:
        candidate += timedelta(days=1)
    # Skip weekends and holidays
    while candidate.weekday() >= 5 or is_holiday(candidate):
        candidate += timedelta(days=1)
    delta = candidate - et
    return max(0, int(delta.total_seconds() // 60))


def minutes_until_close(dt: datetime | None = None) -> int | None:
    """Return minutes until regular session close. None if market is closed."""
    et = (dt or et_now()).astimezone(_ET)
    if not is_market_open(et):
        return None
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = close - et
    return max(0, int(delta.total_seconds() // 60))


def market_session_info(dt: datetime | None = None) -> dict:
    """Return a comprehensive market session info dict.

    Keys:
        label          : str   — 'market_open' | 'pre_market' | 'after_hours' | 'closed'
        et_time_str    : str   — '09:45 ET Mon Jan 15 2026'
        tradeable      : bool  — True if regular session is active
        min_until_open : int|None
        min_until_close: int|None
        context_string : str   — human-readable string for agent prompts
    """
    et = (dt or et_now()).astimezone(_ET)
    label = market_session_label(et)
    tradeable = label == "market_open"

    day_str = et.strftime("%a %b %-d %Y")
    time_str = et.strftime("%H:%M")
    et_time_str = f"{time_str} ET {day_str}"

    min_open = minutes_until_open(et)
    min_close = minutes_until_close(et)

    if tradeable:
        session_desc = f"OPEN (closes in {min_close} min)"
    elif label == "pre_market":
        session_desc = f"PRE-MARKET (opens in {min_open} min)"
    elif label == "after_hours":
        session_desc = f"AFTER-HOURS (next open in {min_open} min)"
    else:
        session_desc = f"CLOSED (next open in {min_open} min)"

    context_string = f"US Market: {session_desc} | Current time: {et_time_str}"

    return {
        "label": label,
        "et_time_str": et_time_str,
        "tradeable": tradeable,
        "min_until_open": min_open,
        "min_until_close": min_close,
        "context_string": context_string,
    }
