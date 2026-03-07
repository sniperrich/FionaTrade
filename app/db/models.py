from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawItem(Base):
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_tier: Mapped[int] = mapped_column(Integer, default=2)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    item_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    tickers: Mapped[list] = mapped_column(JSON, default=list, index=False)
    severity: Mapped[int] = mapped_column(Integer, default=50)
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0, index=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    conflict_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    signaled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    evidence: Mapped[list["EventEvidence"]] = relationship("EventEvidence", back_populates="event")


class EventEvidence(Base):
    __tablename__ = "event_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), index=True)
    url: Mapped[str] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_tier: Mapped[int] = mapped_column(Integer, default=2)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    summary: Mapped[str] = mapped_column(Text, default="")

    event: Mapped[Event] = relationship("Event", back_populates="evidence")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[int] = mapped_column(Integer, index=True)
    horizon_min: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True, nullable=True)
    side: Mapped[str] = mapped_column(String(16), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTED", index=True)


class PaperFill(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    side: Mapped[str] = mapped_column(String(16), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    filled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fill_price: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("ticker", name="uq_position_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)


class Bar1m(Base):
    __tablename__ = "bars_1m"
    __table_args__ = (
        UniqueConstraint("ticker", "ts", name="uq_bar_ticker_ts"),
        Index("ix_bars_1m_ticker_ts", "ticker", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(64), default="finnhub")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    trade_log: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_backtest_trades_run_id_ts", "run_id", "entry_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    entry_ts: Mapped[datetime] = mapped_column(DateTime)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    event_type: Mapped[str] = mapped_column(String(128), default="")


class IngestionCursor(Base):
    __tablename__ = "ingestion_cursors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cursor_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    cursor_value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class SourceStatus(Base):
    __tablename__ = "source_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_name: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="OFFLINE", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


Index("ix_raw_items_source_published_at", RawItem.source, RawItem.published_at)
Index("ix_events_event_time_confidence", Event.event_time, Event.confidence)
Index("ix_source_status_source_name_status", SourceStatus.source_name, SourceStatus.status)
