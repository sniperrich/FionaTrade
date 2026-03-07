from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RawNewsItem(BaseModel):
    source: str
    url: str
    title: str
    body: str
    published_at: datetime
    ingested_at: datetime
    hash: str
    source_tier: int = 2
    metadata: dict = Field(default_factory=dict)


class CanonicalEvent(BaseModel):
    event_type: str
    entities: list[str]
    tickers: list[str]
    severity: int = 50
    event_time: datetime
    evidence_refs: list[int] = Field(default_factory=list)
    summary: str = ""


class ValidatedEvent(BaseModel):
    canonical_event: CanonicalEvent
    confidence: int
    validation_status: Literal["VALID", "WATCH", "REJECTED"]
    conflict_reason: str | None = None


class TradeSignal(BaseModel):
    action: Literal["BUY", "SELL", "SHORT", "HOLD"]
    ticker: str
    confidence: int
    horizon_min: int
    horizon_profile: Literal["SHORT", "MID", "LONG"] | None = None
    reason: str
    expires_at: datetime
    fallback_used: bool = False


class PaperOrderPayload(BaseModel):
    side: Literal["BUY", "SELL", "SHORT", "COVER"]
    qty: float
    submitted_at: datetime


class PaperFillPayload(BaseModel):
    side: Literal["BUY", "SELL", "SHORT", "COVER"]
    qty: float
    submitted_at: datetime
    filled_at: datetime
    fill_price: float
    slippage_bps: float
    fee: float


class BacktestRunPayload(BaseModel):
    params: dict
    metrics: dict
    equity_curve: list[dict]
    trade_log: list[dict]
