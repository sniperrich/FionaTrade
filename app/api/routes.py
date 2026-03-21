from __future__ import annotations

from datetime import datetime
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db
from app.backtest_engine.service import BacktestEngineService
from app.core.config import Settings
from app.core.utils import utc_now
from app.db.models import BacktestRun, Event, EventEvidence, RawItem, Signal, SourceStatus
from app.market.backfill import MarketBackfillService
from app.monitoring.health import HealthAuditService
from app.services.orchestrator import PipelineOrchestrator

router = APIRouter(prefix="/api", tags=["api"])
_RUNTIME_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl_seconds: float) -> Any | None:
    now = time.time()
    row = _RUNTIME_CACHE.get(key)
    if not row:
        return None
    ts, value = row
    if now - ts > ttl_seconds:
        _RUNTIME_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _RUNTIME_CACHE[key] = (time.time(), value)
    return value


def _cache_invalidate(prefix: str) -> None:
    for key in list(_RUNTIME_CACHE.keys()):
        if key.startswith(prefix):
            _RUNTIME_CACHE.pop(key, None)


@router.get("/health")
def health(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    cached = _cache_get("health:snapshot", ttl_seconds=5.0)
    if cached is not None:
        return cached
    audit = HealthAuditService(settings).snapshot(session)
    llm_configured = bool(settings.llm_base_url and settings.llm_model)
    sources_online = sum(1 for s in (audit.get("sources") or {}).values() if s.get("status") == "ONLINE")
    last_ingest = audit.get("last_ingest_age_s")
    return _cache_set("health:snapshot", {
        "status": audit["status"],
        "app": settings.app_name,
        "time": utc_now(),
        "llm_configured": llm_configured,
        "llm_model": settings.llm_model if llm_configured else None,
        "llm_base_url": settings.llm_base_url if llm_configured else None,
        "sources_online": sources_online,
        "last_ingest_age_s": last_ingest,
        "analysis_mode": "llm" if llm_configured else "rules_fallback",
        "audit": audit,
    })


@router.post("/ingest/run")
def run_ingest(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    orchestrator = PipelineOrchestrator(settings)
    return orchestrator.run_ingestion_validation(session)


@router.get("/events")
def list_events(
    session: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    status: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    stmt = select(Event).order_by(Event.event_time.desc()).limit(limit)
    if status:
        stmt = select(Event).where(Event.validation_status == status).order_by(Event.event_time.desc()).limit(limit)

    rows = session.execute(stmt).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        evidence = session.execute(
            select(EventEvidence).where(EventEvidence.event_id == row.id).order_by(EventEvidence.id.asc())
        ).scalars().all()
        out.append(
            {
                "id": row.id,
                "event_type": row.event_type,
                "tickers": row.tickers,
                "severity": row.severity,
                "event_time": row.event_time,
                "confidence": row.confidence,
                "validation_status": row.validation_status,
                "conflict_reason": row.conflict_reason,
                "summary": row.summary,
                "evidence": [
                    {
                        "id": e.id,
                        "url": e.url,
                        "source": e.source,
                        "source_tier": e.source_tier,
                        "captured_at": e.captured_at,
                        "summary": e.summary,
                    }
                    for e in evidence
                ],
            }
        )
    return out


@router.get("/news")
def list_news(
    session: Session = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=2000),
    since_id: int | None = Query(default=None, ge=0),
    before_id: int | None = Query(default=None, ge=1),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1),
) -> dict[str, Any]:
    if since_id is not None and before_id is not None:
        raise HTTPException(status_code=400, detail="since_id and before_id cannot be used together")

    stmt = select(RawItem)
    if source:
        stmt = stmt.where(RawItem.source == source.strip().lower())
    if q:
        keyword = f"%{q.strip()}%"
        stmt = stmt.where(or_(RawItem.title.ilike(keyword), RawItem.body.ilike(keyword)))

    mode = "latest"
    if since_id is not None:
        mode = "newer"
        stmt = stmt.where(RawItem.id > since_id).order_by(RawItem.id.asc()).limit(limit)
    elif before_id is not None:
        mode = "older"
        stmt = stmt.where(RawItem.id < before_id).order_by(RawItem.id.desc()).limit(limit)
    else:
        stmt = stmt.order_by(RawItem.id.desc()).limit(limit)

    rows = session.execute(stmt).scalars().all()

    latest_id = max((row.id for row in rows), default=since_id or 0)
    oldest_id = min((row.id for row in rows), default=before_id or 0)

    has_more_older = False
    if rows:
        more_stmt = select(RawItem.id)
        if source:
            more_stmt = more_stmt.where(RawItem.source == source.strip().lower())
        if q:
            keyword = f"%{q.strip()}%"
            more_stmt = more_stmt.where(or_(RawItem.title.ilike(keyword), RawItem.body.ilike(keyword)))
        more_stmt = more_stmt.where(RawItem.id < oldest_id).limit(1)
        has_more_older = session.execute(more_stmt).first() is not None

    return {
        "mode": mode,
        "latest_id": latest_id,
        "oldest_id": oldest_id,
        "has_more_older": has_more_older,
        "count": len(rows),
        "items": [
            {
                "id": row.id,
                "source": row.source,
                "source_tier": row.source_tier,
                "title": row.title,
                "url": row.url,
                "published_at": row.published_at,
                "ingested_at": row.ingested_at,
                "processed": row.processed,
                "metadata": row.metadata_json or {},
                "body_preview": (row.body or "")[:600],
            }
            for row in rows
        ],
    }


@router.get("/news/sources/status")
def list_news_source_status(
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = session.execute(
        select(SourceStatus).order_by(SourceStatus.source_type.asc(), SourceStatus.display_name.asc())
    ).scalars().all()

    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.source_name not in summary:
            summary[row.source_name] = {
                "status": row.status,
                "error_message": row.error_message,
            }
            continue

        if row.status == "OFFLINE":
            summary[row.source_name]["status"] = "OFFLINE"
            summary[row.source_name]["error_message"] = row.error_message

    return {
        "count": len(rows),
        "summary": summary,
        "items": [
            {
                "source_key": row.source_key,
                "source_name": row.source_name,
                "source_type": row.source_type,
                "display_name": row.display_name,
                "status": row.status,
                "error_message": row.error_message,
                "details": row.details_json,
                "last_checked_at": row.last_checked_at,
                "last_success_at": row.last_success_at,
            }
            for row in rows
        ],
    }


@router.post("/signals/run")
def run_signals(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    orchestrator = PipelineOrchestrator(settings)
    return orchestrator.run_signals(session)


@router.get("/signals")
def list_signals(
    session: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    active_only: bool = Query(default=False),
) -> list[dict[str, Any]]:
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    if active_only:
        stmt = select(Signal).where(and_(Signal.status == "ACTIVE", Signal.expires_at > utc_now())).order_by(
            Signal.created_at.desc()
        ).limit(limit)

    rows = session.execute(stmt).scalars().all()
    return [
        {
            "id": s.id,
            "event_id": s.event_id,
            "action": s.action,
            "ticker": s.ticker,
            "confidence": s.confidence,
            "horizon_min": s.horizon_min,
            "reason": s.reason,
            "expires_at": s.expires_at,
            "fallback_used": s.fallback_used,
            "status": s.status,
            "created_at": s.created_at,
            "executed_at": s.executed_at,
        }
        for s in rows
    ]


@router.post("/paper/execute")
def execute_paper(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    orchestrator = PipelineOrchestrator(settings)
    return orchestrator.run_paper_execution(session)


@router.post("/market/backfill")
def run_market_backfill(
    payload: dict[str, Any] = Body(default_factory=dict),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="start_date and end_date are required")

    tickers = payload.get("tickers")
    if tickers is not None and not isinstance(tickers, list):
        raise HTTPException(status_code=400, detail="tickers must be a list of symbols")

    chunk_days = int(payload.get("chunk_days", 5))
    if chunk_days < 1 or chunk_days > 31:
        raise HTTPException(status_code=400, detail="chunk_days must be between 1 and 31")

    service = MarketBackfillService(settings)
    try:
        result = service.run(
            session,
            start_date=str(start_date),
            end_date=str(end_date),
            tickers=tickers,
            chunk_days=chunk_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


@router.get("/paper/portfolio")
def paper_portfolio(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    orchestrator = PipelineOrchestrator(settings)
    return orchestrator.portfolio(session)


@router.post("/backtests/run")
def run_backtest(
    payload: dict[str, Any] = Body(default_factory=dict),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    engine = BacktestEngineService(settings)
    result = engine.run(session, params=payload)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "metrics": result.metrics,
    }


@router.get("/backtests/{run_id}")
def get_backtest(
    run_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    engine = BacktestEngineService(settings)
    row = engine.get_run(session, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="backtest run not found")

    return {
        "id": row.id,
        "status": row.status,
        "params": row.params,
        "metrics": row.metrics,
        "equity_curve": row.equity_curve,
        "trade_log": row.trade_log,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


# ── Agent endpoints ────────────────────────────────────────────────────────────

@router.post("/agent/run")
def run_agent_graph(
    payload: dict[str, Any] = Body(default={}),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Trigger the multi-agent graph for one or more tickers.

    Body (all optional):
        tickers: list[str]   — override configured tickers for this run
    """
    from app.agent_graph.graph import AgentGraph

    tickers: list[str] = payload.get("tickers") or list(
        settings.agent_tickers_override
        or getattr(settings, "sp100_tickers", [])
        or []
    )
    if not tickers:
        raise HTTPException(status_code=400, detail="No tickers configured or provided")

    graph = AgentGraph(settings)
    runs = []
    for ticker in tickers:
        state = graph.run(session, ticker)
        runs.append({
            "ticker": ticker,
            "action": state.get("final_action", "HOLD"),
            "position_pct": state.get("final_position_pct", 0.0),
            "reasoning": state.get("final_reasoning", ""),
            "error": state.get("error"),
        })

    session.commit()
    return {"agent_mode": True, "runs": runs}


@router.get("/agent/runs")
def list_agent_runs(
    ticker: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return recent AgentRun records."""
    from app.db.models import AgentRun
    from sqlalchemy import select, desc

    stmt = select(AgentRun).order_by(desc(AgentRun.created_at)).offset(offset).limit(limit)
    if ticker:
        stmt = stmt.where(AgentRun.ticker == ticker.upper())
    if action:
        stmt = stmt.where(AgentRun.final_action == action.upper())

    rows = session.execute(stmt).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "ticker": r.ticker,
                "final_action": r.final_action,
                "final_position_pct": r.final_position_pct,
                "final_reasoning": r.final_reasoning,
                "execution_time_ms": r.execution_ms,
                "created_at": r.created_at,
                "macro_result": r.macro_output,
                "news_result": r.news_output,
                "fundamentals_result": r.fundamentals_output,
                "technicals_result": r.technicals_output,
                "risk_result": r.risk_output,
                "portfolio_result": r.portfolio_output,
            }
            for r in rows
        ]
    }


@router.get("/agent/runs/{run_id}")
def get_agent_run(
    run_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return a single AgentRun by ID."""
    from app.db.models import AgentRun
    from sqlalchemy import select

    row = session.execute(select(AgentRun).where(AgentRun.id == run_id)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="agent run not found")
    return {
        "id": row.id,
        "ticker": row.ticker,
        "final_action": row.final_action,
        "final_position_pct": row.final_position_pct,
        "final_reasoning": row.final_reasoning,
        "execution_time_ms": row.execution_ms,
        "created_at": row.created_at,
        "macro_result": row.macro_output,
        "news_result": row.news_output,
        "fundamentals_result": row.fundamentals_output,
        "technicals_result": row.technicals_output,
        "risk_result": row.risk_output,
        "portfolio_result": row.portfolio_output,
    }


@router.post("/agent/macro/refresh")
def refresh_macro_indicators(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Manually trigger FRED macro indicator refresh."""
    from app.ingestion.service import IngestionService
    svc = IngestionService(settings)
    result = svc.refresh_macro_indicators(session)
    session.commit()
    return result


@router.post("/agent/fundamentals/refresh")
def refresh_fundamentals(
    payload: dict[str, Any] = Body(default={}),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Manually trigger fundamentals + analyst ratings refresh."""
    from app.ingestion.service import IngestionService
    tickers = payload.get("tickers") or None
    svc = IngestionService(settings)
    result = svc.refresh_fundamentals_batch(session, tickers)
    session.commit()
    return result


# ── Live Trading ──────────────────────────────────────────────────────────────

@router.get("/live/status")
def live_status(
    settings: Settings = Depends(get_app_settings),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return current live-trading status (enabled, market hours, recent cycle)."""
    from app.core.market_hours import market_session_info
    from app.db.models import LiveTrade
    from sqlalchemy import select, desc

    cached = _cache_get("live:status", ttl_seconds=5.0)
    if cached is not None:
        return cached

    msi = market_session_info()

    # Most recent cycle
    last_trade = session.execute(
        select(LiveTrade).order_by(desc(LiveTrade.id)).limit(1)
    ).scalar_one_or_none()

    last_cycle: dict | None = None
    if last_trade:
        last_cycle = {
            "cycle_id": last_trade.cycle_id,
            "et_time": last_trade.et_time,
            "created_at": last_trade.created_at.isoformat() if last_trade.created_at else None,
        }

    return _cache_set("live:status", {
        "enabled": settings.live_trading_enabled,
        "market_session": msi["label"],
        "market_tradeable": msi["tradeable"],
        "market_time": msi["et_time_str"],
        "market_context": msi["context_string"],
        "cycle_interval_seconds": settings.live_cycle_interval_seconds,
        "max_position_pct": settings.live_max_position_pct,
        "tickers": settings.live_trading_tickers or list(settings.agent_tickers_override or []),
        "last_cycle": last_cycle,
    })


@router.post("/live/cycle")
def trigger_live_cycle(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Manually trigger one live-trading cycle (ignores market hours gate)."""
    from app.services.live_trading import LiveTradingService
    svc = LiveTradingService(settings)
    result = svc.run_cycle(session)
    session.commit()
    _cache_invalidate("live:")
    _cache_invalidate("ui:")
    return result


@router.get("/live/trades")
def list_live_trades(
    ticker: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return recent live trades, newest first."""
    from app.db.models import LiveTrade
    from sqlalchemy import select, desc

    q = select(LiveTrade).order_by(desc(LiveTrade.id)).offset(offset).limit(limit)
    if ticker:
        q = q.where(LiveTrade.ticker == ticker.upper())
    rows = session.execute(q).scalars().all()
    return [
        {
            "id": r.id,
            "cycle_id": r.cycle_id,
            "ticker": r.ticker,
            "action": r.action,
            "quantity": r.quantity,
            "target_pct": r.target_pct,
            "order_id": r.order_id,
            "status": r.status,
            "fill_price": r.fill_price,
            "et_time": r.et_time,
            "market_session": r.market_session,
            "reasoning": r.reasoning,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/live/positions")
def live_positions(
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return current Alpaca positions and account state."""
    from app.broker.alpaca import AlpacaBroker
    cached = _cache_get("live:positions", ttl_seconds=5.0)
    if cached is not None:
        return cached
    broker = AlpacaBroker(settings)
    try:
        account = broker.get_account()
        positions = broker.get_all_positions()
        return _cache_set(
            "live:positions",
            {
            "equity": float(account.get("equity", 0)),
            "cash": float(account.get("cash", 0)),
            "buying_power": float(account.get("buying_power", 0)),
            "positions": [
                {
                    "ticker": p.ticker,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "market_value": p.market_value,
                    "unrealized_pnl": p.unrealized_pnl,
                    "side": "long" if p.quantity > 0 else "short",
                }
                for p in positions
            ],
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


# ── Manual order placement ───────────────────────────────────────────────

@router.post("/live/order")
def place_manual_order(
    body: dict = Body(...),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Place a manual order via Alpaca. Supports market, limit, stop, bracket, notional."""
    from app.broker.alpaca import AlpacaBroker
    broker = AlpacaBroker(settings)
    ticker = (body.get("ticker") or "").upper()
    action = (body.get("action") or "BUY").upper()
    order_type = (body.get("order_type") or "market").lower()
    tif = body.get("time_in_force", "day")

    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    try:
        if order_type == "bracket":
            tp = body.get("take_profit_price")
            sl = body.get("stop_loss_price")
            if not tp or not sl:
                raise HTTPException(status_code=400, detail="bracket order requires take_profit_price and stop_loss_price")
            result = broker.place_bracket_order(
                ticker=ticker, action=action,
                quantity=float(body.get("quantity", 1)),
                take_profit_price=float(tp), stop_loss_price=float(sl),
                time_in_force=tif,
            )
        elif order_type == "notional":
            result = broker.place_notional_order(
                ticker=ticker, action=action,
                notional=float(body.get("notional", body.get("quantity", 100))),
                time_in_force=tif,
            )
        else:
            result = broker.place_order(
                ticker=ticker, action=action,
                quantity=float(body.get("quantity", 1)),
                order_type=order_type,
                limit_price=body.get("limit_price"),
                stop_price=body.get("stop_price"),
                time_in_force=tif,
            )

        if not result.success:
            raise HTTPException(status_code=502, detail=result.error or "Order failed")
        _cache_invalidate("live:")
        _cache_invalidate("ui:")
        return {"success": True, "order_id": result.order_id, "ticker": ticker, "action": action}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/live/open_orders")
def get_open_orders(
    ticker: str | None = Query(default=None),
    settings: Settings = Depends(get_app_settings),
) -> list[dict[str, Any]]:
    """Return all currently open Alpaca orders."""
    from app.broker.alpaca import AlpacaBroker
    cache_key = f"live:open_orders:{(ticker or '').upper()}"
    cached = _cache_get(cache_key, ttl_seconds=5.0)
    if cached is not None:
        return cached
    broker = AlpacaBroker(settings)
    try:
        return _cache_set(cache_key, broker.get_open_orders(ticker=ticker))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.post("/live/cancel_order/{order_id}")
def cancel_order(
    order_id: str,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Cancel a specific open order by ID."""
    from app.broker.alpaca import AlpacaBroker
    broker = AlpacaBroker(settings)
    try:
        success = broker.cancel_order(order_id)
        _cache_invalidate("live:open_orders")
        _cache_invalidate("ui:")
        return {"success": success, "order_id": order_id}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.post("/live/cancel_all_orders")
def cancel_all_orders(
    ticker: str | None = Query(default=None),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Cancel all open orders (optionally for a specific ticker)."""
    from app.broker.alpaca import AlpacaBroker
    broker = AlpacaBroker(settings)
    try:
        cancelled = broker.cancel_all_orders(ticker=ticker)
        _cache_invalidate("live:open_orders")
        _cache_invalidate("ui:")
        return {"success": True, "cancelled": cancelled}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.post("/live/close_position")
def close_position_endpoint(
    ticker: str = Query(...),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Close the full position for a ticker at market price."""
    from app.broker.alpaca import AlpacaBroker
    broker = AlpacaBroker(settings)
    try:
        result = broker.close_position(ticker.upper())
        _cache_invalidate("live:positions")
        _cache_invalidate("live:open_orders")
        _cache_invalidate("live:portfolio_history")
        _cache_invalidate("ui:")
        return {"success": True, "ticker": ticker.upper(), "result": result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.get("/live/latest_price")
def get_latest_price(
    ticker: str = Query(...),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return the latest trade price for a ticker from Alpaca."""
    from app.broker.alpaca import AlpacaBroker
    cache_key = f"live:latest_price:{ticker.upper()}"
    cached = _cache_get(cache_key, ttl_seconds=3.0)
    if cached is not None:
        return cached
    broker = AlpacaBroker(settings)
    try:
        price = broker.get_latest_price(ticker.upper())
        return _cache_set(cache_key, {"ticker": ticker.upper(), "price": price})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.get("/live/bars")
def get_live_bars(
    ticker: str = Query(...),
    timeframe: str = Query(default="5Min"),
    limit: int = Query(default=78, ge=10, le=500),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return recent Alpaca bars for UI charting."""
    from app.broker.alpaca import AlpacaBroker

    normalized_ticker = ticker.upper()
    cache_key = f"live:bars:{normalized_ticker}:{timeframe}:{limit}"
    cached = _cache_get(cache_key, ttl_seconds=10.0)
    if cached is not None:
        return cached

    broker = AlpacaBroker(settings)
    try:
        bars = broker.get_bars(normalized_ticker, timeframe=timeframe, limit=limit)
        payload = {
            "ticker": normalized_ticker,
            "timeframe": timeframe,
            "count": len(bars),
            "bars": [
                {
                    "t": bar.get("t"),
                    "o": float(bar.get("o", 0.0)),
                    "h": float(bar.get("h", 0.0)),
                    "l": float(bar.get("l", 0.0)),
                    "c": float(bar.get("c", 0.0)),
                    "v": float(bar.get("v", 0.0)),
                }
                for bar in bars
            ],
        }
        return _cache_set(cache_key, payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.get("/live/portfolio_history")
def portfolio_history(
    period: str = Query(default="1M"),
    timeframe: str = Query(default="1D"),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return portfolio equity curve from Alpaca (period: 1D/1W/1M/3M/6M/1A, timeframe: 1D/1H/15Min)."""
    from app.broker.alpaca import AlpacaBroker
    cache_key = f"live:portfolio_history:{period}:{timeframe}"
    cached = _cache_get(cache_key, ttl_seconds=15.0)
    if cached is not None:
        return cached
    broker = AlpacaBroker(settings)
    try:
        return _cache_set(cache_key, broker.get_portfolio_history(period=period, timeframe=timeframe))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker error: {exc}")


@router.get("/ui/dashboard_snapshot")
def dashboard_snapshot(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    portfolio_period: str = Query(default="1M"),
    portfolio_timeframe: str = Query(default="1D"),
) -> dict[str, Any]:
    """Aggregate dashboard data into one request to reduce WebUI waterfall latency."""
    cache_key = f"ui:dashboard_snapshot:{portfolio_period}:{portfolio_timeframe}"
    cached = _cache_get(cache_key, ttl_seconds=8.0)
    if cached is not None:
        return cached

    payload: dict[str, Any] = {
        "generated_at": utc_now(),
        "health": None,
        "live": None,
        "positions": None,
        "portfolio_history": None,
        "agent_runs": {"runs": []},
        "news": {"items": []},
        "errors": {},
    }

    try:
        payload["health"] = health(session=session, settings=settings)
    except Exception as exc:
        payload["errors"]["health"] = str(exc)

    try:
        payload["live"] = live_status(settings=settings, session=session)
    except Exception as exc:
        payload["errors"]["live"] = str(exc)

    try:
        payload["positions"] = live_positions(settings=settings)
    except Exception as exc:
        payload["errors"]["positions"] = getattr(exc, "detail", str(exc))

    try:
        payload["portfolio_history"] = portfolio_history(
            period=portfolio_period,
            timeframe=portfolio_timeframe,
            settings=settings,
        )
    except Exception as exc:
        payload["errors"]["portfolio_history"] = getattr(exc, "detail", str(exc))

    try:
        payload["agent_runs"] = list_agent_runs(ticker=None, limit=8, session=session, settings=settings)
    except Exception as exc:
        payload["errors"]["agent_runs"] = str(exc)

    try:
        payload["news"] = list_news(
            session=session,
            limit=6,
            since_id=None,
            before_id=None,
            source=None,
            q=None,
        )
    except Exception as exc:
        payload["errors"]["news"] = str(exc)

    return _cache_set(cache_key, payload)


@router.get("/ui/live_snapshot")
def live_snapshot(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    trade_ticker: str | None = Query(default=None),
    trade_offset: int = Query(default=0, ge=0),
    trade_limit: int = Query(default=20, ge=1, le=200),
    chart_ticker: str | None = Query(default=None),
    chart_timeframe: str = Query(default="5Min"),
    chart_limit: int = Query(default=72, ge=10, le=500),
    portfolio_period: str = Query(default="1M"),
    portfolio_timeframe: str = Query(default="1D"),
) -> dict[str, Any]:
    """Aggregate live-trading data into one request for fast page hydration."""
    selected_chart_ticker = (
        (chart_ticker or "").upper().strip()
        or next(iter(settings.live_trading_tickers or list(settings.agent_tickers_override or [])), "AAPL")
    )
    cache_key = (
        f"ui:live_snapshot:{(trade_ticker or '').upper()}:{trade_offset}:{trade_limit}:{selected_chart_ticker}:"
        f"{chart_timeframe}:{chart_limit}:{portfolio_period}:{portfolio_timeframe}"
    )
    cached = _cache_get(cache_key, ttl_seconds=6.0)
    if cached is not None:
        return cached

    payload: dict[str, Any] = {
        "generated_at": utc_now(),
        "market": None,
        "positions": None,
        "orders": [],
        "trades": [],
        "portfolio_history": None,
        "bars": None,
        "errors": {},
    }

    try:
        payload["market"] = live_status(settings=settings, session=session)
    except Exception as exc:
        payload["errors"]["market"] = str(exc)

    try:
        payload["positions"] = live_positions(settings=settings)
    except Exception as exc:
        payload["errors"]["positions"] = getattr(exc, "detail", str(exc))

    try:
        payload["orders"] = get_open_orders(ticker=None, settings=settings)
    except Exception as exc:
        payload["errors"]["orders"] = getattr(exc, "detail", str(exc))

    try:
        payload["trades"] = list_live_trades(
            ticker=trade_ticker,
            limit=trade_limit,
            offset=trade_offset,
            session=session,
        )
    except Exception as exc:
        payload["errors"]["trades"] = str(exc)

    try:
        payload["portfolio_history"] = portfolio_history(
            period=portfolio_period,
            timeframe=portfolio_timeframe,
            settings=settings,
        )
    except Exception as exc:
        payload["errors"]["portfolio_history"] = getattr(exc, "detail", str(exc))

    try:
        payload["bars"] = get_live_bars(
            ticker=selected_chart_ticker,
            timeframe=chart_timeframe,
            limit=chart_limit,
            settings=settings,
        )
    except Exception as exc:
        payload["errors"]["bars"] = getattr(exc, "detail", str(exc))

    return _cache_set(cache_key, payload)


@router.post("/live/set_enabled")
@router.put("/live/set_enabled")
def set_live_enabled(
    body: dict = Body(default={}),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Toggle live trading on/off at runtime (resets on server restart).
    Set LIVE_TRADING_ENABLED=true in .env for persistence.
    """
    import app.main as _main_module

    was_enabled = bool(settings.live_trading_enabled)
    enabled = bool(body.get("enabled", True))
    settings.live_trading_enabled = enabled
    _cache_invalidate("live:")
    _cache_invalidate("ui:")

    sched = getattr(_main_module, "scheduler", None)
    if sched and sched.running:
        from app.main import _scheduled_live_trading, _scheduled_bar_refresh  # type: ignore[attr-defined]
        if enabled:
            # Add jobs if not already present
            job_ids = {j.id for j in sched.get_jobs()}
            if "live_cycle" not in job_ids:
                sched.add_job(
                    _scheduled_live_trading,
                    "interval",
                    seconds=max(60, settings.live_cycle_interval_seconds),
                    max_instances=1,
                    id="live_cycle",
                )
            if "bar_refresh" not in job_ids:
                sched.add_job(
                    _scheduled_bar_refresh,
                    "interval",
                    minutes=20,
                    max_instances=1,
                    id="bar_refresh",
                )
        else:
            for jid in ("live_cycle", "bar_refresh"):
                try:
                    sched.remove_job(jid)
                except Exception:
                    pass

    if enabled and not was_enabled:
        try:
            import threading
            from app.main import _startup_backfill_bars  # type: ignore[attr-defined]

            threading.Thread(
                target=_startup_backfill_bars,
                daemon=True,
                name="live-enable-bar-backfill",
            ).start()
        except Exception:
            pass

    return {
        "enabled": enabled,
        "message": (
            f"Live trading {'enabled' if enabled else 'disabled'} for this session. "
            + (
                "Started background bar backfill. "
                if enabled and not was_enabled else ""
            )
            + f"To persist, set LIVE_TRADING_ENABLED={'true' if enabled else 'false'} in .env"
        ),
    }
