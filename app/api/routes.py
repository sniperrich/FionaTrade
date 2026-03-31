from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy import and_, desc, distinct, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_app_settings, get_db
from app.core.config import Settings, get_settings
from app.core.utils import ensure_utc, utc_now
from app.backtest_engine.service import BacktestEngineService
from app.agents.reward import batch_score_runs
from app.db.database import db_backend_name, db_connection_ok, is_sqlite_lock_error
from app.db.models import BacktestRun, EntryPlan, Event, EventEvidence, LiveTrade, RawItem, SourceStatus, WorkerRunEvent
from app.monitoring.health import HealthAuditService
from app.services.env_settings import EnvSettingsService
from app.services.market_data import MarketDataService
from app.services.module_attribution import ModuleAttributionService
from app.services.overnight_risk import OvernightRiskService
from app.services.runtime_control import CONTROL_OVERNIGHT_RISK_STATE, RuntimeControlService
from app.services.worker_runtime import (
    COMMAND_REFRESH_BARS,
    COMMAND_RUN_BACKTEST,
    COMMAND_RUN_INGESTION,
    COMMAND_RUN_LIVE_CYCLE,
    WorkerRuntimeService,
)

router = APIRouter(prefix="/api", tags=["api"])
_RUNTIME_CACHE: dict[str, tuple[float, Any]] = {}


def _retry_db_write(
    operation,
    *,
    bind,
    attempts: int = 8,
    base_sleep: float = 0.25,
):
    last_exc: Exception | None = None
    writer_factory = sessionmaker(
        bind=bind,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    for attempt in range(attempts):
        try:
            write_session = writer_factory()
            try:
                result = operation(write_session)
                write_session.commit()
                return result
            except Exception:
                write_session.rollback()
                raise
            finally:
                write_session.close()
        except OperationalError as exc:
            if not is_sqlite_lock_error(exc):
                raise
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_sleep * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("db write retry failed without captured exception")


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


def _normalize_source_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip().lower() for item in value]
    else:
        items = [str(value).strip().lower()]
    return [item for item in items if item]


def _resolve_live_disable_mode(body: dict[str, Any], settings: Settings) -> str:
    return OvernightRiskService.normalize_disable_mode(
        body.get("disable_mode"),
        default=getattr(settings, "live_disable_default_mode", "CANCEL_ORDERS"),
    )


def _normalize_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        chunks = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        chunks = []
        for item in value:
            chunks.extend(str(item).split(","))
    else:
        chunks = [str(value)]
    out: list[int] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return sorted(set(out))


def _normalize_upper_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        chunks = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        chunks = []
        for item in value:
            chunks.extend(str(item).split(","))
    else:
        chunks = [str(value)]
    return sorted({chunk.upper() for chunk in chunks if chunk and str(chunk).strip()})


def _parse_query_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return ensure_utc(dt)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        return ensure_utc(dt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid datetime/date: {value}") from exc


def _news_backfill_meta(
    published_at: datetime | None,
    ingested_at: datetime | None,
    *,
    threshold_minutes: int,
) -> tuple[bool, int | None]:
    if not published_at or not ingested_at:
        return False, None
    delay_seconds = (ensure_utc(ingested_at) - ensure_utc(published_at)).total_seconds()
    delay_minutes = max(0, int(delay_seconds // 60))
    return delay_minutes >= max(1, threshold_minutes), delay_minutes


def _serialize_backtest_run(run: BacktestRun, include_detail: bool = False) -> dict[str, Any]:
    params = run.params or {}
    metrics = run.metrics or {}
    payload = {
        "id": run.id,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "start_date": params.get("start_date"),
        "end_date": params.get("end_date"),
        "use_llm": bool(params.get("use_llm", False)),
        "event_profile": params.get("event_profile") or "",
        "sources": params.get("sources") or [],
        "min_confidence": params.get("min_confidence"),
        "metrics": {
            "trades": metrics.get("trades", 0),
            "events_considered": metrics.get("events_considered", 0),
            "total_return": metrics.get("total_return", 0.0),
            "win_rate": metrics.get("win_rate", 0.0),
            "sharpe": metrics.get("sharpe", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "profit_factor": metrics.get("profit_factor", 0.0),
            "llm_signals": metrics.get("llm_signals", 0),
            "profile_filtered": metrics.get("profile_filtered", 0),
            "tradeability_filtered": metrics.get("tradeability_filtered", 0),
            "validation_blocked": metrics.get("validation_blocked", 0),
            "progress_current": metrics.get("progress_current", 0),
            "progress_total": metrics.get("progress_total", 0),
            "progress_pct": metrics.get("progress_pct", 0.0),
            "trades_so_far": metrics.get("trades_so_far", 0),
            "phase": metrics.get("phase", "queued"),
            "phase_label": metrics.get("phase_label", "Queued"),
            "phase_current": metrics.get("phase_current", 0),
            "phase_total": metrics.get("phase_total", 0),
            "phase_pct": metrics.get("phase_pct", 0.0),
            "phase_detail": metrics.get("phase_detail"),
            "last_progress_at": metrics.get("last_progress_at"),
        },
    }
    if include_detail:
        payload["params"] = params
        payload["metrics_full"] = metrics
        payload["equity_curve"] = run.equity_curve or []
        payload["trade_log"] = run.trade_log or []
    return payload


def _serialize_entry_plan(plan: EntryPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "ticker": plan.ticker,
        "status": plan.status,
        "execution_mode": plan.execution_mode,
        "planned_action": plan.planned_action,
        "target_pct": plan.target_pct,
        "trigger": plan.trigger_json or {},
        "anchor_price": plan.anchor_price,
        "valid_until": plan.valid_until.isoformat() if plan.valid_until else None,
        "trigger_reason": plan.trigger_reason,
        "agent_run_id": plan.agent_run_id,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "triggered_at": plan.triggered_at.isoformat() if plan.triggered_at else None,
        "cancelled_at": plan.cancelled_at.isoformat() if plan.cancelled_at else None,
    }


def _serialize_entry_plan_event(event: WorkerRunEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    return {
        "id": event.id,
        "run_key": event.run_key,
        "level": event.level,
        "stage": event.stage,
        "ticker": event.ticker,
        "agent": event.agent,
        "message": event.message,
        "plan_id": payload.get("plan_id"),
        "status": payload.get("status"),
        "event": payload.get("event"),
        "trigger_reason": payload.get("trigger_reason"),
        "reason": payload.get("reason"),
        "error": payload.get("error"),
        "payload": payload,
        "ts": event.created_at.isoformat() if event.created_at else None,
    }


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
        "db_backend": db_backend_name(),
        "db_connection_ok": db_connection_ok(),
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
    command = WorkerRuntimeService().queue_command(
        session,
        COMMAND_RUN_INGESTION,
        payload={"trigger": "api"},
        requested_by="api",
    )
    session.commit()
    return {
        "queued": True,
        "command_id": command.id,
        "command_type": command.command_type,
        "message": "Ingestion job queued for worker",
    }


@router.get("/settings/editable")
def get_editable_settings(
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    snapshot = EnvSettingsService().snapshot(settings)
    snapshot["worker_restart_required"] = True
    snapshot["web_hot_reload"] = True
    return snapshot


@router.post("/settings/editable")
def save_editable_settings(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    updates = body.get("updates") or {}
    extra_updates = body.get("extra_updates") or ""
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="updates must be an object")
    if extra_updates is not None and not isinstance(extra_updates, str):
        raise HTTPException(status_code=400, detail="extra_updates must be a string")

    service = EnvSettingsService()
    try:
        result = service.apply_updates(updates=updates, extra_updates_text=extra_updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    get_settings.cache_clear()
    refreshed = get_settings()
    _cache_invalidate("health:")
    _cache_invalidate("live:")
    _cache_invalidate("ui:")
    _cache_invalidate("settings:")

    return {
        "saved": True,
        "env_file": result.get("env_file"),
        "written_keys": result.get("written_keys", []),
        "settings": service.snapshot(refreshed).get("editable", {}),
        "web_hot_reload": True,
        "worker_restart_required": True,
        "message": (
            "Saved to .env. Web API will use new values on next request; "
            "worker/supervisor should be restarted to fully apply background-task settings."
        ),
    }


@router.get("/news")
def list_news(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    limit: int = Query(default=200, ge=1, le=2000),
    since_id: int | None = Query(default=None, ge=0),
    before_id: int | None = Query(default=None, ge=1),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1),
    purpose: str = Query(default="all"),
) -> dict[str, Any]:
    if since_id is not None and before_id is not None:
        raise HTTPException(status_code=400, detail="since_id and before_id cannot be used together")
    normalized_purpose = str(purpose or "all").strip().lower()
    if normalized_purpose not in {"all", "raw", "event", "agent", "live"}:
        raise HTTPException(status_code=400, detail="purpose must be one of: all, raw, event, agent, live")

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
        stmt = stmt.order_by(RawItem.published_at.desc(), RawItem.id.desc()).limit(limit)

    rows = session.execute(stmt).scalars().all()
    row_ids = [int(row.id) for row in rows]
    backfill_threshold_minutes = max(1, int(settings.news_backfill_delay_minutes))

    raw_event_map: dict[int, set[int]] = {}
    event_agent_map: dict[int, set[int]] = {}
    agent_live_map: dict[int, set[int]] = {}
    if row_ids:
        evidence_links = session.execute(
            select(EventEvidence.raw_item_id, EventEvidence.event_id).where(EventEvidence.raw_item_id.in_(row_ids))
        ).all()
        event_ids: set[int] = set()
        for raw_item_id, event_id in evidence_links:
            raw_id_i = int(raw_item_id)
            event_id_i = int(event_id)
            raw_event_map.setdefault(raw_id_i, set()).add(event_id_i)
            event_ids.add(event_id_i)

        if event_ids:
            from app.db.models import AgentRun

            agent_links = session.execute(
                select(AgentRun.id, AgentRun.trigger_event_id).where(AgentRun.trigger_event_id.in_(event_ids))
            ).all()
            agent_ids: set[int] = set()
            for agent_run_id, trigger_event_id in agent_links:
                if trigger_event_id is None:
                    continue
                event_id_i = int(trigger_event_id)
                agent_id_i = int(agent_run_id)
                event_agent_map.setdefault(event_id_i, set()).add(agent_id_i)
                agent_ids.add(agent_id_i)

            if agent_ids:
                live_links = session.execute(
                    select(LiveTrade.id, LiveTrade.agent_run_id).where(LiveTrade.agent_run_id.in_(agent_ids))
                ).all()
                for live_trade_id, agent_run_id in live_links:
                    if agent_run_id is None:
                        continue
                    agent_live_map.setdefault(int(agent_run_id), set()).add(int(live_trade_id))

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
        if mode == "latest":
            last_row = rows[-1]
            more_stmt = more_stmt.where(
                or_(
                    RawItem.published_at < last_row.published_at,
                    and_(RawItem.published_at == last_row.published_at, RawItem.id < last_row.id),
                )
            )
        else:
            more_stmt = more_stmt.where(RawItem.id < oldest_id)
        more_stmt = more_stmt.limit(1)
        has_more_older = session.execute(more_stmt).first() is not None

    def _purpose_layer(raw_item_id: int) -> tuple[str, list[int], list[int], list[int]]:
        event_ids = sorted(raw_event_map.get(raw_item_id, set()))
        agent_ids: set[int] = set()
        for event_id in event_ids:
            agent_ids.update(event_agent_map.get(event_id, set()))
        live_ids: set[int] = set()
        for agent_id in agent_ids:
            live_ids.update(agent_live_map.get(agent_id, set()))
        agent_id_list = sorted(agent_ids)
        live_id_list = sorted(live_ids)
        if live_id_list:
            layer = "LIVE_TRADE_INPUT"
        elif agent_id_list:
            layer = "AGENT_TRIGGER_INPUT"
        elif event_ids:
            layer = "EVENT_EVIDENCE"
        else:
            layer = "RAW_INGEST_ONLY"
        return layer, event_ids, agent_id_list, live_id_list

    def _purpose_match(layer: str) -> bool:
        if normalized_purpose == "all":
            return True
        if normalized_purpose == "raw":
            return layer == "RAW_INGEST_ONLY"
        if normalized_purpose == "event":
            return layer in {"EVENT_EVIDENCE", "AGENT_TRIGGER_INPUT", "LIVE_TRADE_INPUT"}
        if normalized_purpose == "agent":
            return layer in {"AGENT_TRIGGER_INPUT", "LIVE_TRADE_INPUT"}
        if normalized_purpose == "live":
            return layer == "LIVE_TRADE_INPUT"
        return True

    purpose_counts = {
        "RAW_INGEST_ONLY": 0,
        "EVENT_EVIDENCE": 0,
        "AGENT_TRIGGER_INPUT": 0,
        "LIVE_TRADE_INPUT": 0,
    }
    items_payload: list[dict[str, Any]] = []
    for row in rows:
        layer, linked_event_ids, linked_agent_run_ids, linked_live_trade_ids = _purpose_layer(int(row.id))
        purpose_counts[layer] = int(purpose_counts.get(layer, 0)) + 1
        if not _purpose_match(layer):
            continue
        historical_backfill, backfill_delay_min = _news_backfill_meta(
            row.published_at,
            row.ingested_at,
            threshold_minutes=backfill_threshold_minutes,
        )
        items_payload.append(
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
                "historical_backfill": historical_backfill,
                "backfill_delay_min": backfill_delay_min,
                "purpose_layer": layer,
                "linked_event_ids": linked_event_ids,
                "linked_agent_run_ids": linked_agent_run_ids,
                "linked_live_trade_ids": linked_live_trade_ids,
                "is_event_evidence": bool(linked_event_ids),
                "is_agent_input": bool(linked_agent_run_ids),
                "is_live_input": bool(linked_live_trade_ids),
            }
        )

    return {
        "mode": mode,
        "purpose": normalized_purpose,
        "latest_id": latest_id,
        "oldest_id": oldest_id,
        "has_more_older": has_more_older,
        "count": len(items_payload),
        "total_rows": len(rows),
        "purpose_counts": purpose_counts,
        "items": items_payload,
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


@router.get("/backtests/options")
def backtest_options(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    source_rows = session.execute(
        select(distinct(EventEvidence.source)).order_by(EventEvidence.source.asc())
    ).scalars().all()
    return {
        "sources": [row for row in source_rows if row],
        "event_profiles": [
            {"value": "", "label": "All Events"},
            {"value": "earnings_only", "label": "Earnings Only"},
        ],
        "defaults": {
            "start_date": (utc_now() - timedelta(days=30)).date().isoformat(),
            "end_date": utc_now().date().isoformat(),
            "use_llm": False,
            "event_profile": "",
            "sources": [],
            "min_confidence": settings.min_trade_confidence,
            "min_severity": 0,
            "use_signal_validation": bool(getattr(settings, "validation_enabled", True)),
            "use_tradeability_filter": settings.event_tradeability_filter_enabled,
            "use_event_quality_filter": settings.backtest_use_event_quality_filter,
            "flow_confirmation_enabled": bool(getattr(settings, "flow_confirmation_enabled", True)),
            "flow_confirmation_soft_gate": bool(getattr(settings, "flow_confirmation_soft_gate", True)),
            "flow_breakout_lookback_min": int(getattr(settings, "live_entry_plan_breakout_lookback_min", 15)),
            "flow_wait_valid_minutes": int(getattr(settings, "live_entry_plan_default_valid_minutes", 180)),
        },
    }


@router.get("/backtests")
def list_backtests(
    session: Session = Depends(get_db),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(BacktestRun)
    count_stmt = select(func.count(BacktestRun.id))
    if status:
        normalized_status = status.strip().upper()
        stmt = stmt.where(BacktestRun.status == normalized_status)
        count_stmt = count_stmt.where(BacktestRun.status == normalized_status)

    total_count = int(session.execute(count_stmt).scalar_one() or 0)
    rows = session.execute(
        stmt.order_by(desc(BacktestRun.created_at), desc(BacktestRun.id)).offset(offset).limit(limit)
    ).scalars().all()
    return {
        "total_count": total_count,
        "items": [_serialize_backtest_run(row) for row in rows],
    }


@router.post("/backtests/run")
def queue_backtest(
    payload: dict[str, Any] = Body(default_factory=dict),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="start_date and end_date are required")
    try:
        start_dt = datetime.fromisoformat(str(start_date))
        end_dt = datetime.fromisoformat(str(end_date))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date range: {exc}") from exc
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    params = {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "use_llm": bool(payload.get("use_llm", False)),
        "event_profile": str(payload.get("event_profile") or "").strip().lower(),
        "sources": _normalize_source_list(payload.get("sources")),
        "min_confidence": int(payload.get("min_confidence", settings.min_trade_confidence)),
        "min_severity": int(payload.get("min_severity", 0)),
        "use_signal_validation": bool(payload.get("use_signal_validation", getattr(settings, "validation_enabled", True))),
        "use_tradeability_filter": bool(payload.get("use_tradeability_filter", settings.event_tradeability_filter_enabled)),
        "use_event_quality_filter": bool(payload.get("use_event_quality_filter", settings.backtest_use_event_quality_filter)),
        "flow_confirmation_enabled": bool(payload.get("flow_confirmation_enabled", getattr(settings, "flow_confirmation_enabled", True))),
        "flow_confirmation_soft_gate": bool(payload.get("flow_confirmation_soft_gate", getattr(settings, "flow_confirmation_soft_gate", True))),
        "flow_breakout_lookback_min": int(payload.get("flow_breakout_lookback_min", getattr(settings, "live_entry_plan_breakout_lookback_min", 15))),
        "flow_wait_valid_minutes": int(payload.get("flow_wait_valid_minutes", getattr(settings, "live_entry_plan_default_valid_minutes", 180))),
        "trigger": "api",
    }

    run = BacktestRun(
        params=params,
        status="QUEUED",
        metrics={
            "phase": "queued",
            "phase_label": "Queued",
            "phase_current": 0,
            "phase_total": 0,
            "phase_pct": 0.0,
            "phase_detail": "Waiting for worker to claim command",
            "last_progress_at": utc_now().isoformat(),
        },
    )
    session.add(run)
    session.flush()

    command = WorkerRuntimeService().queue_command(
        session,
        COMMAND_RUN_BACKTEST,
        payload={**params, "backtest_run_id": run.id},
        requested_by="api",
    )
    session.commit()
    _cache_invalidate("ui:backtests")

    return {
        "queued": True,
        "run_id": run.id,
        "command_id": command.id,
        "status": run.status,
        "message": "Backtest queued for worker",
    }


@router.get("/backtests/{run_id}")
def get_backtest_run(
    run_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    row = BacktestEngineService(settings).get_run(session, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return _serialize_backtest_run(row, include_detail=True)


@router.get("/attribution/overview")
def attribution_overview(
    session: Session = Depends(get_db),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    lookback_days: int = Query(default=30, ge=1, le=3650),
    mode: str = Query(default="all"),
    run_ids: str | None = Query(default=None),
    tickers: str | None = Query(default=None),
    min_sample: int = Query(default=5, ge=1, le=200),
) -> dict[str, Any]:
    start_dt = _parse_query_dt(start_date)
    end_dt = _parse_query_dt(end_date)
    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")
    normalized_run_ids = _normalize_int_list(run_ids)
    normalized_tickers = _normalize_upper_list(tickers)

    cache_key = (
        "attribution:overview:"
        f"{start_date or ''}:{end_date or ''}:{lookback_days}:{mode}:"
        f"{','.join(str(v) for v in normalized_run_ids)}:{','.join(normalized_tickers)}:{min_sample}"
    )
    cached = _cache_get(cache_key, ttl_seconds=10.0)
    if cached is not None:
        return cached

    payload = ModuleAttributionService().overview(
        session,
        start_date=start_dt,
        end_date=end_dt,
        lookback_days=lookback_days,
        mode=mode,
        run_ids=normalized_run_ids,
        tickers=normalized_tickers,
        min_sample=min_sample,
    )
    return _cache_set(cache_key, payload)


@router.get("/attribution/runs/{run_id}")
def attribution_run_detail(
    run_id: int,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = ModuleAttributionService().run_detail(session, run_id=run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return payload


@router.post("/attribution/agent-scores/backfill")
def attribution_backfill_agent_scores(
    body: dict[str, Any] = Body(default_factory=dict),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    lookback_days = max(1, int(body.get("lookback_days", 30)))
    eval_horizon_days = max(1, int(body.get("eval_horizon_days", 3)))
    limit = max(1, min(20000, int(body.get("limit", 2000))))
    as_of = utc_now()
    since = as_of - timedelta(days=lookback_days)
    created = batch_score_runs(
        session,
        since=since,
        eval_horizon_days=eval_horizon_days,
        as_of=as_of,
        limit=limit,
    )
    session.commit()
    _cache_invalidate("attribution:")
    return {
        "ok": True,
        "created_scores": created,
        "lookback_days": lookback_days,
        "eval_horizon_days": eval_horizon_days,
        "limit": limit,
        "as_of": as_of.isoformat(),
    }


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

    command = WorkerRuntimeService().queue_command(
        session,
        COMMAND_REFRESH_BARS,
        payload={
            "start_date": str(start_date),
            "end_date": str(end_date),
            "tickers": tickers,
            "chunk_days": chunk_days,
            "sleep_seconds": float(payload.get("sleep_seconds", 0.12)),
            "trigger": "api",
        },
        requested_by="api",
    )
    session.commit()
    return {
        "queued": True,
        "command_id": command.id,
        "command_type": command.command_type,
        "message": "Market backfill job queued for worker",
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
    run_ids = [int(r.id) for r in rows]
    trigger_event_ids = sorted({int(r.trigger_event_id) for r in rows if r.trigger_event_id is not None})

    event_map: dict[int, dict[str, Any]] = {}
    event_evidence_count: dict[int, int] = {}
    if trigger_event_ids:
        event_rows = session.execute(
            select(
                Event.id,
                Event.event_type,
                Event.event_time,
                Event.confidence,
                Event.validation_status,
                Event.summary,
            ).where(Event.id.in_(trigger_event_ids))
        ).all()
        for event_id, event_type, event_time, confidence, validation_status, summary in event_rows:
            event_map[int(event_id)] = {
                "id": int(event_id),
                "event_type": event_type,
                "event_time": event_time.isoformat() if event_time else None,
                "confidence": int(confidence or 0),
                "validation_status": validation_status,
                "summary": summary or "",
            }
        count_rows = session.execute(
            select(EventEvidence.event_id, func.count(EventEvidence.id))
            .where(EventEvidence.event_id.in_(trigger_event_ids))
            .group_by(EventEvidence.event_id)
        ).all()
        event_evidence_count = {int(event_id): int(count) for event_id, count in count_rows}

    live_trade_summary: dict[int, dict[str, Any]] = {}
    if run_ids:
        live_rows = session.execute(
            select(
                LiveTrade.agent_run_id,
                LiveTrade.id,
                LiveTrade.status,
                LiveTrade.created_at,
                LiveTrade.action,
                LiveTrade.quantity,
            )
            .where(LiveTrade.agent_run_id.in_(run_ids))
            .order_by(LiveTrade.agent_run_id.asc(), LiveTrade.created_at.desc(), LiveTrade.id.desc())
        ).all()
        for agent_run_id, trade_id, status_i, created_at_i, action_i, qty_i in live_rows:
            if agent_run_id is None:
                continue
            run_id_i = int(agent_run_id)
            bucket = live_trade_summary.setdefault(
                run_id_i,
                {
                    "count": 0,
                    "latest_trade_id": None,
                    "latest_status": None,
                    "latest_created_at": None,
                    "latest_action": None,
                    "latest_quantity": None,
                },
            )
            bucket["count"] = int(bucket["count"]) + 1
            if bucket["latest_trade_id"] is None:
                bucket["latest_trade_id"] = int(trade_id)
                bucket["latest_status"] = status_i
                bucket["latest_created_at"] = created_at_i.isoformat() if created_at_i else None
                bucket["latest_action"] = action_i
                bucket["latest_quantity"] = qty_i

    return {
        "runs": [
            {
                "id": r.id,
                "ticker": r.ticker,
                "trigger_event_id": r.trigger_event_id,
                "trigger_event": event_map.get(int(r.trigger_event_id)) if r.trigger_event_id is not None else None,
                "trigger_event_evidence_count": int(event_evidence_count.get(int(r.trigger_event_id), 0)) if r.trigger_event_id is not None else 0,
                "final_action": r.final_action,
                "final_confidence": r.final_confidence,
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
                "flow_score": ((r.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("flow_score"),
                "position_multiplier": ((r.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("position_multiplier"),
                "used_cached_macro": ((r.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("used_cached_macro"),
                "used_cached_fundamentals": ((r.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("used_cached_fundamentals"),
                "fast_path": ((r.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("fast_path"),
                "live_trade_summary": live_trade_summary.get(int(r.id), {"count": 0}),
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

    trigger_event_payload: dict[str, Any] | None = None
    if row.trigger_event_id is not None:
        event_row = session.get(Event, int(row.trigger_event_id))
        if event_row is not None:
            evidence_rows = session.execute(
                select(
                    EventEvidence.id,
                    EventEvidence.url,
                    EventEvidence.source,
                    EventEvidence.source_tier,
                    EventEvidence.captured_at,
                    EventEvidence.summary,
                    RawItem.title,
                    RawItem.published_at,
                )
                .join(RawItem, RawItem.id == EventEvidence.raw_item_id, isouter=True)
                .where(EventEvidence.event_id == int(event_row.id))
                .order_by(EventEvidence.source_tier.asc(), EventEvidence.captured_at.desc(), EventEvidence.id.asc())
                .limit(24)
            ).all()
            trigger_event_payload = {
                "id": int(event_row.id),
                "event_type": event_row.event_type,
                "event_time": event_row.event_time.isoformat() if event_row.event_time else None,
                "confidence": int(event_row.confidence or 0),
                "validation_status": event_row.validation_status,
                "summary": event_row.summary or "",
                "evidence": [
                    {
                        "id": int(evidence_id),
                        "url": url,
                        "source": source,
                        "source_tier": int(source_tier or 9),
                        "captured_at": captured_at.isoformat() if captured_at else None,
                        "summary": summary or "",
                        "title": title or "",
                        "published_at": published_at.isoformat() if published_at else None,
                    }
                    for evidence_id, url, source, source_tier, captured_at, summary, title, published_at in evidence_rows
                ],
            }

    linked_live_trades = session.execute(
        select(
            LiveTrade.id,
            LiveTrade.action,
            LiveTrade.quantity,
            LiveTrade.status,
            LiveTrade.market_session,
            LiveTrade.order_id,
            LiveTrade.created_at,
        )
        .where(LiveTrade.agent_run_id == int(row.id))
        .order_by(LiveTrade.created_at.desc(), LiveTrade.id.desc())
        .limit(20)
    ).all()

    return {
        "id": row.id,
        "ticker": row.ticker,
        "trigger_event_id": row.trigger_event_id,
        "trigger_event": trigger_event_payload,
        "final_action": row.final_action,
        "final_confidence": row.final_confidence,
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
        "flow_score": ((row.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("flow_score"),
        "position_multiplier": ((row.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("position_multiplier"),
        "used_cached_macro": ((row.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("used_cached_macro"),
        "used_cached_fundamentals": ((row.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("used_cached_fundamentals"),
        "fast_path": ((row.portfolio_output or {}).get("metadata") or {}).get("live_runtime", {}).get("fast_path"),
        "linked_live_trades": [
            {
                "id": int(trade_id),
                "action": action_i,
                "quantity": qty_i,
                "status": status_i,
                "market_session": market_session_i,
                "order_id": order_id_i,
                "created_at": created_at_i.isoformat() if created_at_i else None,
            }
            for trade_id, action_i, qty_i, status_i, market_session_i, order_id_i, created_at_i in linked_live_trades
        ],
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
    cached = _cache_get("live:status", ttl_seconds=5.0)
    if cached is not None:
        return cached

    msi = market_session_info()
    control = RuntimeControlService()
    runtime = WorkerRuntimeService()
    enabled = control.get_live_enabled(session, settings)
    live_control_row = control.get(session, "live_trading_enabled") or {}
    overnight_state = control.get(session, CONTROL_OVERNIGHT_RISK_STATE) or {}
    worker_bundle = runtime.worker_status_snapshot(session)
    latest_run = WorkerRuntimeService().latest_run(session, "live_cycle")
    last_cycle: dict | None = None
    if latest_run:
        last_cycle = {
            "cycle_id": latest_run.run_key,
            "market_session": latest_run.market_session,
            "created_at": latest_run.started_at.isoformat() if latest_run.started_at else None,
            "updated_at": latest_run.updated_at.isoformat() if latest_run.updated_at else None,
            "status": latest_run.status,
            "stage": latest_run.stage,
        }

    return _cache_set("live:status", {
        "enabled": enabled,
        "market_session": msi["label"],
        "market_tradeable": msi["tradeable"],
        "market_time": msi["et_time_str"],
        "market_context": msi["context_string"],
        "cycle_interval_seconds": settings.live_cycle_interval_seconds,
        "open_cycle_seconds": settings.live_open_cycle_seconds,
        "closed_cycle_seconds": settings.live_closed_cycle_seconds,
        "ticker_cooldown_minutes": settings.live_ticker_cooldown_minutes,
        "max_position_pct": settings.live_max_position_pct,
        "live_min_confidence": int(getattr(settings, "live_min_confidence", settings.min_trade_confidence)),
        "live_disable_default_mode": OvernightRiskService.normalize_disable_mode(
            getattr(settings, "live_disable_default_mode", "CANCEL_ORDERS"),
            default="CANCEL_ORDERS",
        ),
        "last_disable_mode": live_control_row.get("last_disable_mode")
        or OvernightRiskService.normalize_disable_mode(
            getattr(settings, "live_disable_default_mode", "CANCEL_ORDERS"),
            default="CANCEL_ORDERS",
        ),
        "tickers": settings.live_trading_tickers or list(settings.agent_tickers_override or []),
        "live_allowed_sources": list(getattr(settings, "live_allowed_sources", []) or []),
        "live_overnight_risk_enabled": bool(getattr(settings, "live_overnight_risk_enabled", True)),
        "live_overnight_mode": OvernightRiskService.normalize_overnight_mode(
            getattr(settings, "live_overnight_mode", "REDUCE"),
            default="REDUCE",
        ),
        "live_overnight_max_gross_exposure_pct": float(
            getattr(settings, "live_overnight_max_gross_exposure_pct", 0.25) or 0.25
        ),
        "live_overnight_rebalance_minutes_before_close": int(
            getattr(settings, "live_overnight_rebalance_minutes_before_close", 5) or 5
        ),
        "live_overnight_run_when_disabled": bool(
            getattr(settings, "live_overnight_run_when_disabled", True)
        ),
        "overnight_state": overnight_state,
        "enable_finnhub_company_news_live": bool(getattr(settings, "enable_finnhub_company_news_live", True)),
        "finnhub_company_news_live_lookback_days": int(
            getattr(settings, "finnhub_company_news_live_lookback_days", 2) or 2
        ),
        "last_cycle": last_cycle,
        "worker": worker_bundle["worker"],
        "supervisor": worker_bundle["supervisor"],
        "command_queue": worker_bundle["command_queue"],
        "control_plane": {
            "mode": "worker_control_plane",
            "browser_independent": True,
            "requires_worker": True,
        },
    })


@router.get("/live/runtime")
def live_runtime_status(
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return WorkerRuntimeService().runtime_snapshot(session)


@router.get("/worker/status")
def worker_status(
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return WorkerRuntimeService().worker_status_snapshot(session)


@router.get("/worker/history")
def worker_history(
    session: Session = Depends(get_db),
    run_limit: int = Query(default=10, ge=1, le=50),
    command_limit: int = Query(default=10, ge=1, le=50),
    event_limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return WorkerRuntimeService().history_snapshot(
        session,
        run_limit=run_limit,
        command_limit=command_limit,
        event_limit=event_limit,
    )


@router.get("/live/bar_cache")
def live_bar_cache_status(
    ticker: str | None = Query(default=None),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    selected_ticker = (
        (ticker or "").upper().strip()
        or next(iter(settings.live_trading_tickers or list(settings.agent_tickers_override or [])), "AAPL")
    )
    cache_key = f"live:bar_cache:{selected_ticker}"
    cached = _cache_get(cache_key, ttl_seconds=8.0)
    if cached is not None:
        return cached
    return _cache_set(cache_key, MarketDataService(settings).get_bar_cache_status(session, selected_ticker))


@router.post("/live/cycle")
def trigger_live_cycle(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Queue one live-trading cycle for the worker."""
    command = WorkerRuntimeService().queue_command(
        session,
        COMMAND_RUN_LIVE_CYCLE,
        payload={"trigger": "manual"},
        requested_by="api",
    )
    session.commit()
    _cache_invalidate("live:")
    _cache_invalidate("ui:")
    return {
        "queued": True,
        "command_id": command.id,
        "command_type": command.command_type,
        "message": "Live cycle queued for worker",
    }


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


@router.get("/live/plans")
def list_live_entry_plans(
    ticker: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(EntryPlan).order_by(desc(EntryPlan.updated_at), desc(EntryPlan.id)).limit(limit)
    if ticker:
        stmt = stmt.where(EntryPlan.ticker == ticker.upper().strip())
    if status:
        stmt = stmt.where(EntryPlan.status == status.upper().strip())
    rows = session.execute(stmt).scalars().all()
    active_count = sum(1 for row in rows if (row.status or "").upper() == "ACTIVE")
    return {
        "count": len(rows),
        "active_count": active_count,
        "items": [_serialize_entry_plan(row) for row in rows],
    }


@router.get("/live/plans/events")
def list_live_entry_plan_events(
    ticker: str | None = Query(default=None),
    plan_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=60, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    stage_filter = [
        "entry_plan_created",
        "entry_plan_expired",
        "entry_plan_evaluated",
        "entry_plan_triggered",
        "entry_plan_trigger_failed",
        "entry_plan_invalidated",
        "entry_plan_cancelled",
    ]
    ticker_upper = ticker.upper().strip() if ticker else None
    fetch_limit = limit if plan_id is None else min(500, max(limit * 6, limit))
    stmt = (
        select(WorkerRunEvent)
        .where(
            WorkerRunEvent.run_type == "live_cycle",
            WorkerRunEvent.stage.in_(stage_filter),
        )
        .order_by(desc(WorkerRunEvent.created_at), desc(WorkerRunEvent.id))
        .limit(fetch_limit)
    )
    if ticker_upper:
        stmt = stmt.where(WorkerRunEvent.ticker == ticker_upper)
    rows = session.execute(stmt).scalars().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload_json or {})
        row_plan_id = payload.get("plan_id")
        if row_plan_id is not None:
            try:
                row_plan_id = int(row_plan_id)
            except (TypeError, ValueError):
                row_plan_id = None
        if plan_id is not None and row_plan_id != plan_id:
            continue
        items.append(_serialize_entry_plan_event(row))
        if len(items) >= limit:
            break

    return {
        "count": len(items),
        "items": items,
    }


@router.post("/live/plans/{plan_id}/cancel")
def cancel_live_entry_plan(
    plan_id: int,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    plan = session.get(EntryPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"entry plan {plan_id} not found")
    status = (plan.status or "").upper()
    if status != "ACTIVE":
        return {
            "success": False,
            "message": f"Entry plan {plan_id} is already {status or 'UNKNOWN'}",
            "plan": _serialize_entry_plan(plan),
        }
    plan.status = "CANCELLED"
    plan.cancelled_at = utc_now()
    plan.updated_at = utc_now()
    if not plan.trigger_reason:
        plan.trigger_reason = "cancelled from control plane"
    WorkerRuntimeService().add_event(
        session,
        "live_cycle",
        f"Entry plan #{plan.id} cancelled from control plane",
        run=None,
        level="warn",
        stage="entry_plan_cancelled",
        ticker=plan.ticker,
        agent="control_plane",
        payload={
            "event": "entry_plan_cancelled",
            "plan_id": plan.id,
            "status": "cancelled",
            "trigger_reason": plan.trigger_reason,
        },
    )
    session.flush()
    _cache_invalidate("live:")
    _cache_invalidate("ui:")
    return {
        "success": True,
        "message": f"Cancelled entry plan {plan_id}",
        "plan": _serialize_entry_plan(plan),
    }


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
        open_orders = broker.get_open_orders()
        summary = OvernightRiskService.summarize_account(
            account,
            positions,
            open_orders_count=len(open_orders),
            settings=settings,
        )
        return _cache_set(
            "live:positions",
            {
                **summary,
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
        _cache_invalidate("live:positions")
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
        _cache_invalidate("live:positions")
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


@router.post("/live/close_all_positions")
def close_all_positions_endpoint(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Cancel open orders and flatten all positions immediately."""
    try:
        result = OvernightRiskService(settings).flatten_all_positions(
            session,
            reason="manual flatten from control plane; cancel all open orders and flatten all positions",
            cycle_id=f"manual_flatten:{utc_now().strftime('%Y%m%dT%H%M%SZ')}",
            event_stage="manual_flatten",
            requested_by="manual_flatten",
            cancel_orders=True,
        )
        session.commit()
        _cache_invalidate("live:")
        _cache_invalidate("ui:")
        return {"success": True, **result}
    except Exception as exc:
        session.rollback()
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
    source: str = Query(default="auto"),
    limit: int = Query(default=78, ge=10, le=500),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Return recent bars for UI charting via MarketDataService."""
    normalized_ticker = ticker.upper()
    requested_source = source.lower().strip()
    cache_key = f"live:bars:{normalized_ticker}:{timeframe}:{requested_source}:{limit}"
    cached = _cache_get(cache_key, ttl_seconds=10.0)
    if cached is not None:
        return cached

    try:
        payload = MarketDataService(settings).get_chart_bars(
            session,
            normalized_ticker,
            timeframe=timeframe,
            source=requested_source,
            limit=limit,
        )
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
            settings=settings,
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
    chart_source: str = Query(default="auto"),
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
        f"{chart_source}:{chart_timeframe}:{chart_limit}:{portfolio_period}:{portfolio_timeframe}"
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
        "plans": {"count": 0, "active_count": 0, "items": []},
        "plan_events": {"count": 0, "items": []},
        "portfolio_history": None,
        "bars": None,
        "bar_cache": None,
        "runtime": None,
        "worker_history": None,
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
        payload["plans"] = list_live_entry_plans(
            ticker=trade_ticker,
            status=None,
            limit=100,
            session=session,
        )
    except Exception as exc:
        payload["errors"]["plans"] = str(exc)

    try:
        payload["plan_events"] = list_live_entry_plan_events(
            ticker=trade_ticker,
            plan_id=None,
            limit=60,
            session=session,
        )
    except Exception as exc:
        payload["errors"]["plan_events"] = str(exc)

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
            source=chart_source,
            timeframe=chart_timeframe,
            limit=chart_limit,
            session=session,
            settings=settings,
        )
    except Exception as exc:
        payload["errors"]["bars"] = getattr(exc, "detail", str(exc))

    try:
        payload["bar_cache"] = live_bar_cache_status(
            ticker=selected_chart_ticker,
            session=session,
            settings=settings,
        )
    except Exception as exc:
        payload["errors"]["bar_cache"] = str(exc)

    try:
        payload["runtime"] = live_runtime_status(session=session)
    except Exception as exc:
        payload["errors"]["runtime"] = str(exc)

    try:
        payload["worker_history"] = worker_history(session=session, run_limit=8, command_limit=8, event_limit=16)
    except Exception as exc:
        payload["errors"]["worker_history"] = str(exc)

    return _cache_set(cache_key, payload)


@router.post("/live/set_enabled")
@router.put("/live/set_enabled")
def set_live_enabled(
    body: dict = Body(default={}),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Toggle live trading through shared DB runtime control."""
    control = RuntimeControlService()
    enabled = bool(body.get("enabled", True))
    disable_mode = _resolve_live_disable_mode(body, settings) if not enabled else None
    has_tickers = bool(settings.live_trading_tickers or list(settings.agent_tickers_override or []))
    worker_status = control.get_worker_status(session)
    supervisor_status = control.get_supervisor_status(session)

    if enabled:
        if not worker_status.get("online"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "worker is offline or stale; start `python -m app.worker.supervisor` "
                    "or use `./run_local.sh` before enabling live trading"
                ),
            )
        if not supervisor_status.get("online"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "worker supervisor is offline or stale; start `python -m app.worker.supervisor` "
                    "or use `./run_local.sh` before enabling live trading"
                ),
            )

    tickers = [t.upper() for t in (settings.live_trading_tickers or list(settings.agent_tickers_override or [])) if t]

    def _write_toggle(write_session: Session) -> dict[str, Any]:
        write_control = RuntimeControlService()
        write_runtime = WorkerRuntimeService()
        local_was_enabled = write_control.get_live_enabled(write_session, settings)
        write_control.set_live_enabled(
            write_session,
            settings,
            enabled,
            source="api",
            disable_mode=disable_mode,
        )
        cancelled_commands = 0
        if enabled and not local_was_enabled:
            today = utc_now().strftime("%Y-%m-%d")
            tomorrow = (utc_now() + timedelta(days=1)).strftime("%Y-%m-%d")
            write_runtime.queue_command(
                write_session,
                COMMAND_REFRESH_BARS,
                payload={
                    "start_date": today,
                    "end_date": tomorrow,
                    "tickers": tickers,
                    "chunk_days": 1,
                    "sleep_seconds": 0.1,
                    "trigger": "enable_live",
                },
                requested_by="api",
            )
            write_runtime.queue_command(
                write_session,
                COMMAND_RUN_LIVE_CYCLE,
                payload={"trigger": "enable_live"},
                requested_by="api",
            )
        if not enabled:
            cancelled_commands = write_runtime.cancel_pending_commands(
                write_session,
                command_types=[COMMAND_RUN_INGESTION, COMMAND_REFRESH_BARS, COMMAND_RUN_LIVE_CYCLE],
                reason="live trading disabled from control plane",
                trigger="enable_live",
            )
        return {
            "was_enabled": local_was_enabled,
            "cancelled_commands": cancelled_commands,
        }

    try:
        write_result = _retry_db_write(_write_toggle, bind=session.get_bind())
    except OperationalError as exc:
        if is_sqlite_lock_error(exc):
            raise HTTPException(
                status_code=503,
                detail="database is busy; try again in a few seconds",
            ) from exc
        raise

    _cache_invalidate("live:")
    _cache_invalidate("ui:")

    broker_action: dict[str, Any] | None = None
    if not enabled:
        try:
            if disable_mode == "CANCEL_ORDERS":
                from app.broker.alpaca import AlpacaBroker

                cancelled = AlpacaBroker(settings).cancel_all_orders()
                broker_action = {
                    "disable_mode": disable_mode,
                    "cancelled_orders": cancelled,
                    "flattened_positions": 0,
                }
            elif disable_mode == "FLATTEN_ALL":
                broker_action = OvernightRiskService(settings).flatten_all_positions(
                    session,
                    reason="disable live with flatten-all mode; cancel open orders and flatten all positions",
                    cycle_id=f"disable_flatten:{utc_now().strftime('%Y%m%dT%H%M%SZ')}",
                    event_stage="disable_flatten",
                    requested_by="disable_flatten",
                    cancel_orders=True,
                )
                session.commit()
            else:
                broker_action = {
                    "disable_mode": disable_mode,
                    "cancelled_orders": 0,
                    "flattened_positions": 0,
                }
        except Exception as exc:
            session.rollback()
            broker_action = {
                "disable_mode": disable_mode,
                "error": str(exc),
            }

    _cache_invalidate("live:")
    _cache_invalidate("ui:")

    return {
        "enabled": enabled,
        "disable_mode": disable_mode,
        "broker_action": broker_action,
        "message": (
            f"Live trading {'enabled' if enabled else 'disabled'} in shared runtime control. "
            + (
                "Queued bar backfill and an immediate live cycle for worker. "
                if enabled and not write_result.get("was_enabled") else ""
            )
            + (
                "No live tickers are configured yet. "
                if enabled and not has_tickers else ""
            )
            + (
                f"Cancelled {write_result.get('cancelled_commands', 0)} pending live commands. "
                if not enabled and write_result.get("cancelled_commands", 0) else ""
            )
            + (
                f"Disable mode={disable_mode}. "
                if not enabled and disable_mode else ""
            )
            + (
                f"Broker action error: {broker_action.get('error')}. "
                if isinstance(broker_action, dict) and broker_action.get("error") else ""
            )
            + "Worker and supervisor are online. Closing the browser does not stop auto trading."
        ),
    }
