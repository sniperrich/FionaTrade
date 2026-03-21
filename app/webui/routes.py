from __future__ import annotations

from datetime import timedelta
from math import ceil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db
from app.core.config import Settings
from app.core.utils import utc_now
from app.db.models import BacktestRun, Event, EventEvidence, PaperFill, Position, RawItem, Signal, SourceStatus
from app.paper_engine.service import PaperEngineService
from app.services.runtime_control import RuntimeControlService

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["webui"])


def _build_pager(total_count: int, page: int, per_page: int) -> dict:
    total_pages = max(1, ceil(total_count / per_page)) if total_count else 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    start_item = offset + 1 if total_count else 0
    end_item = min(offset + per_page, total_count) if total_count else 0
    return {
        "page": page,
        "per_page": per_page,
        "offset": offset,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "start_item": start_item,
        "end_item": end_item,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_db), settings: Settings = Depends(get_app_settings)):
    now = utc_now()
    one_day_ago = now - timedelta(days=1)

    events_24h = session.execute(select(func.count(Event.id)).where(Event.created_at >= one_day_ago)).scalar_one()
    signals_24h = session.execute(select(func.count(Signal.id)).where(Signal.created_at >= one_day_ago)).scalar_one()
    valid_signals = session.execute(
        select(func.count(Signal.id)).where(and_(Signal.status == "ACTIVE", Signal.expires_at > now))
    ).scalar_one()

    latest_ingest = session.execute(select(func.max(RawItem.ingested_at))).scalar_one()
    source_latency_sec = 0.0
    if latest_ingest:
        from datetime import timezone
        if latest_ingest.tzinfo is None:
            latest_ingest = latest_ingest.replace(tzinfo=timezone.utc)
        source_latency_sec = (now - latest_ingest).total_seconds()

    engine = PaperEngineService(settings)
    portfolio = engine.portfolio(session)

    context = {
        "request": request,
        "title": "Dashboard",
        "events_24h": events_24h,
        "signals_24h": signals_24h,
        "valid_signals": valid_signals,
        "source_latency_sec": round(source_latency_sec, 1),
        "paper_nav": round(portfolio["nav"], 2),
        "paper_realized": round(portfolio["realized_pnl"], 2),
        "paper_unrealized": round(portfolio["unrealized_pnl"], 2),
    }
    return templates.TemplateResponse("dashboard.html", context)


@router.get("/events", response_class=HTMLResponse)
def event_stream(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    session: Session = Depends(get_db),
):
    total_count = session.execute(select(func.count(Event.id))).scalar_one()
    pager = _build_pager(total_count, page, per_page)
    events = session.execute(
        select(Event)
        .order_by(Event.event_time.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    evidence_map: dict[int, list[EventEvidence]] = {}
    for event in events:
        evidence_map[event.id] = (
            session.execute(select(EventEvidence).where(EventEvidence.event_id == event.id).order_by(EventEvidence.id.asc()))
            .scalars()
            .all()
        )

    return templates.TemplateResponse(
        "events.html",
        {
            "request": request,
            "title": "Event Stream",
            "events": events,
            "evidence_map": evidence_map,
            "pager": pager,
        },
    )


@router.get("/news", response_class=HTMLResponse)
def news_stream(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    session: Session = Depends(get_db),
):
    total_count = session.execute(select(func.count(RawItem.id))).scalar_one()
    pager = _build_pager(total_count, page, per_page)
    rows = session.execute(
        select(RawItem)
        .order_by(RawItem.id.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    latest_id = rows[0].id if rows else 0
    oldest_id = rows[-1].id if rows else 0
    source_rows = session.execute(
        select(SourceStatus).order_by(SourceStatus.source_type.asc(), SourceStatus.display_name.asc())
    ).scalars().all()
    source_names = session.execute(select(distinct(RawItem.source)).order_by(RawItem.source.asc())).scalars().all()
    status_summary: dict[str, dict] = {}
    for row in source_rows:
        if row.source_name not in status_summary:
            status_summary[row.source_name] = {
                "status": row.status,
                "error_message": row.error_message,
            }
        elif row.status == "OFFLINE":
            status_summary[row.source_name] = {
                "status": "OFFLINE",
                "error_message": row.error_message,
            }
    return templates.TemplateResponse(
        "news.html",
        {
            "request": request,
            "title": "News Stream",
            "news_items": rows,
            "latest_id": latest_id,
            "oldest_id": oldest_id,
            "source_statuses": source_rows,
            "status_summary": status_summary,
            "source_names": source_names,
            "pager": pager,
        },
    )


@router.get("/signals", response_class=HTMLResponse)
def signals(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    session: Session = Depends(get_db),
):
    total_count = session.execute(select(func.count(Signal.id))).scalar_one()
    pager = _build_pager(total_count, page, per_page)
    rows = session.execute(
        select(Signal)
        .order_by(Signal.created_at.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    return templates.TemplateResponse(
        "signals.html",
        {
            "request": request,
            "title": "Signals",
            "signals": rows,
            "pager": pager,
        },
    )


@router.get("/paper", response_class=HTMLResponse)
def paper_trading(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    engine = PaperEngineService(settings)
    portfolio = engine.portfolio(session)
    total_fills = session.execute(select(func.count(PaperFill.id))).scalar_one()
    pager = _build_pager(total_fills, page, per_page)
    fills = session.execute(
        select(PaperFill)
        .order_by(PaperFill.filled_at.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    positions = session.execute(select(Position).order_by(Position.ticker.asc())).scalars().all()

    return templates.TemplateResponse(
        "paper.html",
        {
            "request": request,
            "title": "Paper Trading",
            "portfolio": portfolio,
            "fills": fills,
            "positions": positions,
            "pager": pager,
        },
    )


@router.get("/backtests", response_class=HTMLResponse)
def backtests(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    session: Session = Depends(get_db),
):
    total_count = session.execute(select(func.count(BacktestRun.id))).scalar_one()
    pager = _build_pager(total_count, page, per_page)
    runs = session.execute(
        select(BacktestRun)
        .order_by(BacktestRun.created_at.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    return templates.TemplateResponse(
        "backtests.html",
        {
            "request": request,
            "title": "Backtests",
            "runs": runs,
            "pager": pager,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, settings: Settings = Depends(get_app_settings)):
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Settings",
            "settings": settings,
        },
    )


@router.get("/agents", response_class=HTMLResponse)
def agents_page(
    request: Request,
    ticker: str | None = None,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    from app.db.models import AgentRun
    from sqlalchemy import desc, distinct, select

    stmt = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(50)
    if ticker:
        stmt = stmt.where(AgentRun.ticker == ticker.upper())
    runs = session.execute(stmt).scalars().all()

    # Collect unique tickers for filter dropdown
    all_tickers = session.execute(
        select(distinct(AgentRun.ticker)).order_by(AgentRun.ticker)
    ).scalars().all()

    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "title": "AI Agents",
            "runs": runs,
            "tickers": all_tickers,
            "selected_ticker": ticker,
            "agent_mode_enabled": settings.agent_mode_enabled,
        },
    )


@router.get("/live", response_class=HTMLResponse)
def live_trading_page(
    request: Request,
    ticker: str | None = None,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    from app.core.market_hours import market_session_info
    from app.db.models import LiveTrade
    from sqlalchemy import select, desc

    msi = market_session_info()

    stmt = select(LiveTrade).order_by(desc(LiveTrade.id)).limit(100)
    if ticker:
        stmt = stmt.where(LiveTrade.ticker == ticker.upper())
    trades = session.execute(stmt).scalars().all()
    live_enabled = RuntimeControlService().get_live_enabled(session, settings)

    return templates.TemplateResponse(
        "live.html",
        {
            "request": request,
            "title": "Live Trading",
            "trades": trades,
            "selected_ticker": ticker,
            "live_enabled": live_enabled,
            "market_session": msi,
            "tickers": settings.live_trading_tickers or list(settings.agent_tickers_override or []),
            "default_chart_ticker": ticker or ((settings.live_trading_tickers or list(settings.agent_tickers_override or []))[:1] or ["AAPL"])[0],
        },
    )
