from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_utc_from_timezone(dt: datetime, tzinfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tzinfo).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def make_hash(*parts: str) -> str:
    payload = "||".join(parts)
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def normalize_title(title: str) -> str:
    stripped = re.sub(r"\s+", " ", title.lower()).strip()
    stripped = re.sub(r"[^a-z0-9\s]", "", stripped)
    return stripped


def minute_bucket(dt: datetime, width_min: int = 30) -> datetime:
    dt = ensure_utc(dt)
    minute = (dt.minute // width_min) * width_min
    return dt.replace(minute=minute, second=0, microsecond=0)
