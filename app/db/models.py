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


class EarningsCalendar(Base):
    __tablename__ = "earnings_calendar"
    __table_args__ = (
        UniqueConstraint("symbol", "report_date", "quarter", "fiscal_year", name="uq_earnings_calendar_symbol_date_qy"),
        Index("ix_earnings_calendar_symbol_report_date", "symbol", "report_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    report_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    report_hour: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="finnhub")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


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


class RuntimeControl(Base):
    __tablename__ = "runtime_controls"
    __table_args__ = (
        UniqueConstraint("control_key", name="uq_runtime_control_key"),
        Index("ix_runtime_controls_key_updated", "control_key", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    control_key: Mapped[str] = mapped_column(String(128), index=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, index=True)


class WorkerCommand(Base):
    __tablename__ = "worker_commands"
    __table_args__ = (
        Index("ix_worker_commands_status_created", "status", "created_at"),
        Index("ix_worker_commands_type_status", "command_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkerRun(Base):
    __tablename__ = "worker_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_worker_runs_run_key"),
        Index("ix_worker_runs_type_started", "run_type", "started_at"),
        Index("ix_worker_runs_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(64), index=True)
    run_type: Mapped[str] = mapped_column(String(64), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued", index=True)
    current_ticker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_session: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    total_tickers: Mapped[int] = mapped_column(Integer, default=0)
    completed_tickers: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkerRunEvent(Base):
    __tablename__ = "worker_run_events"
    __table_args__ = (
        Index("ix_worker_run_events_run_created", "run_id", "created_at"),
        Index("ix_worker_run_events_type_created", "run_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("worker_runs.id"), nullable=True, index=True)
    run_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_type: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# ── New tables for V2 multi-agent system ─────────────────────────────────────


class MacroIndicator(Base):
    """FRED macroeconomic time-series observations."""

    __tablename__ = "macro_indicators"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_date", name="uq_macro_series_date"),
        Index("ix_macro_indicators_series_date", "series_id", "observation_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(32), index=True)
    indicator_name: Mapped[str] = mapped_column(String(128))
    observation_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(64), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class FundamentalsSnapshot(Base):
    """Per-ticker fundamental financial metrics (quarterly/annual snapshots)."""

    __tablename__ = "fundamentals_snapshots"
    __table_args__ = (
        UniqueConstraint("ticker", "period", "period_type", name="uq_fundamentals_ticker_period"),
        Index("ix_fundamentals_ticker_period", "ticker", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    period: Mapped[str] = mapped_column(String(16))       # e.g. "2024Q4", "2024"
    period_type: Mapped[str] = mapped_column(String(8))   # "quarterly" or "annual"

    # Income statement
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_income: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Key ratios (from Finnhub /stock/metric)
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    ps_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    roa: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    operating_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_52_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_52_low: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="finnhub")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class AnalystRating(Base):
    """Analyst consensus ratings and price targets per ticker."""

    __tablename__ = "analyst_ratings"
    __table_args__ = (
        UniqueConstraint("ticker", "period", name="uq_analyst_rating_ticker_period"),
        Index("ix_analyst_ratings_ticker_period", "ticker", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    period: Mapped[str] = mapped_column(String(16))  # e.g. "2024-12"

    # Analyst consensus counts (from Finnhub /stock/recommendation)
    strong_buy: Mapped[int] = mapped_column(Integer, default=0)
    buy: Mapped[int] = mapped_column(Integer, default=0)
    hold: Mapped[int] = mapped_column(Integer, default=0)
    sell: Mapped[int] = mapped_column(Integer, default=0)
    strong_sell: Mapped[int] = mapped_column(Integer, default=0)

    # Price target (from Finnhub /stock/price-target)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_at_fetch: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="finnhub")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class AgentRun(Base):
    """Records each Agent Graph execution with per-agent reasoning traces."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_ticker_created_at", "ticker", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled")  # "scheduled" | "manual" | "event"
    trigger_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Per-agent outputs stored as JSON blobs
    macro_output: Mapped[dict] = mapped_column(JSON, default=dict)
    news_output: Mapped[dict] = mapped_column(JSON, default=dict)
    fundamentals_output: Mapped[dict] = mapped_column(JSON, default=dict)
    technicals_output: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_output: Mapped[dict] = mapped_column(JSON, default=dict)
    portfolio_output: Mapped[dict] = mapped_column(JSON, default=dict)

    # Final decision
    final_action: Mapped[str | None] = mapped_column(String(16), nullable=True)  # BUY/SHORT/HOLD/NO_TRADE
    final_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_reasoning: Mapped[str] = mapped_column(Text, default="")

    # Linked signal if one was generated
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(16), default="COMPLETED", index=True)  # COMPLETED | FAILED | SKIPPED
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class AgentScore(Base):
    """Tracks each agent's prediction accuracy for reward/penalty feedback.

    After a prediction is made, the scorer evaluates what actually happened
    to the stock and assigns a score (-100 to +100). This feeds back into
    agent prompts and dynamic weight adjustment.
    """

    __tablename__ = "agent_scores"
    __table_args__ = (
        Index("ix_agent_scores_agent_ticker", "agent_name", "ticker"),
        Index("ix_agent_scores_scored_at", "scored_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)

    # What the agent predicted
    signal: Mapped[str] = mapped_column(String(16))  # BUY / SHORT / HOLD / NO_SIGNAL
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    predicted_at: Mapped[datetime] = mapped_column(DateTime)

    # What actually happened
    price_at_prediction: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    eval_horizon_days: Mapped[int] = mapped_column(Integer, default=3)

    # Score: -100 (terrible) to +100 (perfect)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_reasoning: Mapped[str] = mapped_column(Text, default="")

    scored_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LiveTrade(Base):
    """Records every order placed by the live trading cycle.

    One row per order. Status progresses: submitted → filled | cancelled | error.
    The cycle_id groups all orders placed in the same trading cycle run.
    """

    __tablename__ = "live_trades"
    __table_args__ = (
        Index("ix_live_trades_ticker_created", "ticker", "created_at"),
        Index("ix_live_trades_cycle_id", "cycle_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Cycle that triggered this trade (UUID string)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ticker: Mapped[str] = mapped_column(String(16))

    # Reference to the AgentRun that produced the decision
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Decision fields
    action: Mapped[str] = mapped_column(String(16))           # BUY | SELL | SHORT | COVER | HOLD
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    target_pct: Mapped[float] = mapped_column(Float, default=0.0)   # fraction of portfolio

    # Broker fields
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_qty: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Market context at time of decision
    et_time: Mapped[str] = mapped_column(String(48), default="")      # "09:45 ET Mon Jan 15 2026"
    market_session: Mapped[str] = mapped_column(String(32), default="") # "market_open" | "pre_market" …

    # Agent reasoning (trimmed)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

