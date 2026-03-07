from __future__ import annotations

from app.analysis.taxonomy import NEGATIVE_EVENTS, POSITIVE_EVENTS


def fallback_action(event_type: str) -> str:
    if event_type in POSITIVE_EVENTS:
        return "BUY"
    if event_type in NEGATIVE_EVENTS:
        return "SHORT"
    return "HOLD"
