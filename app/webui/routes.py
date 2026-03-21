from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db
from app.core.config import Settings
from app.core.utils import utc_now
from app.db.models import BacktestRun, Event, EventEvidence, PaperFill, Position, RawItem, Signal, SourceStatus
from app.paper_engine.service import PaperEngineService

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["webui"])


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
def event_stream(request: Request, session: Session = Depends(get_db)):
    events = session.execute(select(Event).order_by(Event.event_time.desc()).limit(200)).scalars().all()
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
        },
    )


@router.get("/news", response_class=HTMLResponse)
def news_stream(request: Request, session: Session = Depends(get_db)):
    rows = session.execute(select(RawItem).order_by(RawItem.id.desc()).limit(200)).scalars().all()
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
        },
    )


@router.get("/signals", response_class=HTMLResponse)
def signals(request: Request, session: Session = Depends(get_db)):
    rows = session.execute(select(Signal).order_by(Signal.created_at.desc()).limit(200)).scalars().all()
    return templates.TemplateResponse(
        "signals.html",
        {
            "request": request,
            "title": "Signals",
            "signals": rows,
        },
    )


@router.get("/paper", response_class=HTMLResponse)
def paper_trading(request: Request, session: Session = Depends(get_db), settings: Settings = Depends(get_app_settings)):
    engine = PaperEngineService(settings)
    portfolio = engine.portfolio(session)
    fills = session.execute(select(PaperFill).order_by(PaperFill.filled_at.desc()).limit(200)).scalars().all()
    positions = session.execute(select(Position).order_by(Position.ticker.asc())).scalars().all()

    return templates.TemplateResponse(
        "paper.html",
        {
            "request": request,
            "title": "Paper Trading",
            "portfolio": portfolio,
            "fills": fills,
            "positions": positions,
        },
    )


@router.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request, session: Session = Depends(get_db)):
    runs = session.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(100)).scalars().all()
    return templates.TemplateResponse(
        "backtests.html",
        {
            "request": request,
            "title": "Backtests",
            "runs": runs,
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
    from sqlalchemy import select, desc

    stmt = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(50)
    if ticker:
        stmt = stmt.where(AgentRun.ticker == ticker.upper())
    runs = session.execute(stmt).scalars().all()

    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "title": "Agent Runs",
            "runs": runs,
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

    return templates.TemplateResponse(
        "live.html",
        {
            "request": request,
            "title": "Live Trading",
            "trades": trades,
            "selected_ticker": ticker,
            "live_enabled": settings.live_trading_enabled,
            "market_session": msi,
            "tickers": settings.live_trading_tickers or list(settings.agent_tickers_override or []),
        },
    )
