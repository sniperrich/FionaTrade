from __future__ import annotations

from datetime import timedelta
from math import ceil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db
from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import AgentRun, BacktestRun, EventEvidence, LiveTrade, RawItem, SourceStatus
from app.services.env_settings import EnvSettingsService
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


def _news_backfill_meta(
    published_at,
    ingested_at,
    *,
    threshold_minutes: int,
) -> dict[str, int | bool | None]:
    if not published_at or not ingested_at:
        return {"historical_backfill": False, "backfill_delay_min": None}
    delay_seconds = (ensure_utc(ingested_at) - ensure_utc(published_at)).total_seconds()
    delay_minutes = max(0, int(delay_seconds // 60))
    return {
        "historical_backfill": delay_minutes >= max(1, threshold_minutes),
        "backfill_delay_min": delay_minutes,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_db), settings: Settings = Depends(get_app_settings)):
    now = utc_now()
    one_day_ago = now - timedelta(days=1)

    news_items_24h = session.execute(select(func.count(RawItem.id)).where(RawItem.ingested_at >= one_day_ago)).scalar_one()
    agent_runs_24h = session.execute(select(func.count(AgentRun.id)).where(AgentRun.created_at >= one_day_ago)).scalar_one()
    live_trades_24h = session.execute(select(func.count(LiveTrade.id)).where(LiveTrade.created_at >= one_day_ago)).scalar_one()

    latest_ingest = session.execute(select(func.max(RawItem.ingested_at))).scalar_one()
    source_latency_sec = 0.0
    if latest_ingest:
        from datetime import timezone
        if latest_ingest.tzinfo is None:
            latest_ingest = latest_ingest.replace(tzinfo=timezone.utc)
        source_latency_sec = (now - latest_ingest).total_seconds()

    context = {
        "request": request,
        "title": "Dashboard",
        "news_items_24h": news_items_24h,
        "agent_runs_24h": agent_runs_24h,
        "live_trades_24h": live_trades_24h,
        "source_latency_sec": round(source_latency_sec, 1),
    }
    return templates.TemplateResponse("dashboard.html", context)


@router.get("/news", response_class=HTMLResponse)
def news_stream(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    total_count = session.execute(select(func.count(RawItem.id))).scalar_one()
    pager = _build_pager(total_count, page, per_page)
    rows = session.execute(
        select(RawItem)
        .order_by(RawItem.published_at.desc(), RawItem.id.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    latest_id = max((row.id for row in rows), default=0)
    oldest_id = rows[-1].id if rows else 0
    backfill_threshold_minutes = max(1, int(settings.news_backfill_delay_minutes))
    backfill_meta = {
        row.id: _news_backfill_meta(
            row.published_at,
            row.ingested_at,
            threshold_minutes=backfill_threshold_minutes,
        )
        for row in rows
    }
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
            "news_backfill_delay_minutes": backfill_threshold_minutes,
            "news_backfill_meta": backfill_meta,
            "pager": pager,
        },
    )




@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, settings: Settings = Depends(get_app_settings)):
    editable_snapshot = EnvSettingsService().snapshot(settings)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Settings",
            "settings": settings,
            "settings_editor": editable_snapshot,
        },
    )


@router.get("/agents", response_class=HTMLResponse)
def agents_page(
    request: Request,
    ticker: str | None = None,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
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


@router.get("/backtests", response_class=HTMLResponse)
def backtests_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=5, le=100),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    total_count = int(session.execute(select(func.count(BacktestRun.id))).scalar_one() or 0)
    pager = _build_pager(total_count, page, per_page)
    rows = session.execute(
        select(BacktestRun)
        .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
        .offset(pager["offset"])
        .limit(pager["per_page"])
    ).scalars().all()
    sources = session.execute(
        select(distinct(EventEvidence.source)).order_by(EventEvidence.source.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        "backtests.html",
        {
            "request": request,
            "title": "Backtests",
            "backtest_runs": rows,
            "backtest_sources": [source for source in sources if source],
            "backtest_defaults": {
                "start_date": (utc_now() - timedelta(days=30)).date().isoformat(),
                "end_date": utc_now().date().isoformat(),
                "use_llm": False,
                "event_profile": "",
                "min_confidence": settings.min_trade_confidence,
                "min_severity": 0,
                "flow_confirmation_enabled": bool(getattr(settings, "flow_confirmation_enabled", True)),
                "flow_confirmation_soft_gate": bool(getattr(settings, "flow_confirmation_soft_gate", True)),
                "flow_breakout_lookback_min": int(getattr(settings, "live_entry_plan_breakout_lookback_min", 15)),
                "flow_wait_valid_minutes": int(getattr(settings, "live_entry_plan_default_valid_minutes", 180)),
            },
            "pager": pager,
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
