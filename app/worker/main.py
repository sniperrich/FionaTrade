from __future__ import annotations

import os
import signal
import socket
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.core.logging import get_app_logger, log_writeout, setup_logging
from app.core.market_hours import is_market_open
from app.db.database import db_session, init_db
from app.ingestion.service import IngestionService
from app.monitoring.health import HealthAuditService
from app.services.earnings_calendar import EarningsCalendarService
from app.services.live_trading import LiveTradingService
from app.services.market_data import MarketDataService
from app.services.runtime_control import RuntimeControlService
from app.services.worker_runtime import (
    COMMAND_REFRESH_BARS,
    COMMAND_REFRESH_EARNINGS,
    COMMAND_RUN_INGESTION,
    COMMAND_RUN_LIVE_CYCLE,
    WorkerRuntimeService,
)

settings = get_settings()
setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)
logger = get_app_logger()
runtime_control = RuntimeControlService()
runtime = WorkerRuntimeService()
market_data = MarketDataService(settings)
scheduler: BackgroundScheduler | None = None
_shutdown = threading.Event()
_WORKER_STARTED_AT = datetime.now(timezone.utc)
_WORKER_HOST = socket.gethostname()


def _is_live_enabled() -> bool:
    with db_session() as session:
        return runtime_control.get_live_enabled(session, settings)


def _scheduled_ingestion() -> None:
    try:
        with db_session() as session:
            ingest_result = IngestionService(settings).run(session)
            log_writeout(
                "ingestion_tick",
                {
                    "ingestion": asdict(ingest_result),
                },
            )
            logger.info(
                "采集完成 fetched=%s inserted=%s duplicates=%s",
                ingest_result.fetched,
                ingest_result.inserted,
                ingest_result.duplicate_dropped,
            )
    except Exception as exc:
        logger.exception("采集任务失败: %s", exc)


def _scheduled_health_audit() -> None:
    try:
        with db_session() as session:
            report = HealthAuditService(settings).run_and_log(session, scheduler_running=True)
            logger.info("健康巡检完成 status=%s issues=%s", report["status"], report["issues"])
    except Exception as exc:
        logger.exception("健康巡检失败: %s", exc)


def _scheduled_earnings_refresh() -> None:
    try:
        with db_session() as session:
            result = EarningsCalendarService(settings).refresh_if_due(session)
            if result is not None:
                logger.info(
                    "财报日历刷新完成 fetched=%s upserted=%s skipped=%s from=%s to=%s",
                    result.fetched,
                    result.upserted,
                    result.skipped,
                    result.from_date,
                    result.to_date,
                )
    except Exception as exc:
        logger.exception("财报日历刷新失败: %s", exc)


def _scheduled_bar_refresh() -> None:
    if not _is_live_enabled():
        return
    if not is_market_open():
        logger.info("[bar_refresh] skipped: market closed")
        return
    try:
        tickers = market_data.tracked_tickers()
        if not tickers:
            logger.info("[bar_refresh] skipped: no live tickers configured")
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        with db_session() as session:
            result = market_data.refresh_bars(
                session,
                start_date=today,
                end_date=tomorrow,
                tickers=tickers,
                chunk_days=1,
                sleep_seconds=0.1,
                trigger="scheduled",
            )
            logger.info("[bar_refresh] Scheduled refresh complete: %s", result)
    except Exception as exc:
        logger.exception("[bar_refresh] Scheduled refresh failed: %s", exc)


def _startup_backfill_bars() -> None:
    try:
        if not (_is_live_enabled() or settings.agent_mode_enabled):
            logger.info("[bar_refresh] startup skipped: live trading disabled")
            return
        with db_session() as session:
            result = market_data.startup_backfill_if_stale(session, trigger="startup")
            logger.info("[bar_refresh] Startup backfill result: %s", result)
    except Exception as exc:
        logger.exception("[bar_refresh] Startup backfill failed: %s", exc)


def _scheduled_live_trading() -> None:
    if not _is_live_enabled():
        return
    try:
        with db_session() as session:
            result = LiveTradingService(settings).run_cycle(session, trigger="scheduled")
            if result.get("skipped"):
                logger.debug("[live] 模拟盘跳过 reason=%s", result.get("reason"))
            else:
                logger.info(
                    "[live] 模拟盘轮询完成 cycle=%s orders=%s/%s val=$%s%s",
                    result.get("cycle_id"),
                    result.get("orders_placed", 0),
                    result.get("tickers_processed", 0),
                    result.get("portfolio_value", 0),
                    " [ANALYSIS]" if result.get("dry_run") else "",
                )
    except Exception as exc:
        logger.exception("[live] 模拟盘轮询失败: %s", exc)


def _heartbeat_worker() -> None:
    try:
        with db_session() as session:
            enabled = runtime_control.get_live_enabled(session, settings)
            runtime_control.touch_worker_heartbeat(
                session,
                pid=os.getpid(),
                started_at=_WORKER_STARTED_AT,
                source="worker",
                scheduler_running=scheduler is not None,
                extra={
                    "host": _WORKER_HOST,
                    "live_enabled": enabled,
                    "configured_tickers": market_data.tracked_tickers(),
                },
            )
    except Exception as exc:
        logger.exception("[worker] heartbeat failed: %s", exc)


def _process_worker_commands() -> None:
    try:
        while True:
            with db_session() as session:
                command = runtime.claim_next_command(
                    session,
                    [
                        COMMAND_RUN_INGESTION,
                        COMMAND_REFRESH_BARS,
                        COMMAND_RUN_LIVE_CYCLE,
                        COMMAND_REFRESH_EARNINGS,
                    ],
                )
                if command is None:
                    return
                try:
                    payload = command.payload_json or {}
                    if command.command_type == COMMAND_RUN_INGESTION:
                        result = IngestionService(settings).run(session)
                        result = {
                            "fetched": result.fetched,
                            "inserted": result.inserted,
                            "duplicate_dropped": result.duplicate_dropped,
                            "raw_item_ids": result.raw_item_ids,
                        }
                    elif command.command_type == COMMAND_REFRESH_BARS:
                        result = market_data.refresh_bars(
                            session,
                            start_date=str(payload.get("start_date")),
                            end_date=str(payload.get("end_date")),
                            tickers=payload.get("tickers"),
                            chunk_days=int(payload.get("chunk_days", 5)),
                            sleep_seconds=float(payload.get("sleep_seconds", 0.12)),
                            trigger=str(payload.get("trigger", "manual")),
                        )
                    elif command.command_type == COMMAND_REFRESH_EARNINGS:
                        service = EarningsCalendarService(settings)
                        if payload.get("from_date") and payload.get("to_date"):
                            refreshed = service.refresh(
                                session,
                                from_date=str(payload["from_date"]),
                                to_date=str(payload["to_date"]),
                                symbols=payload.get("symbols"),
                            )
                        else:
                            refreshed = service.refresh_if_due(session)
                        result = {
                            "fetched": getattr(refreshed, "fetched", 0),
                            "upserted": getattr(refreshed, "upserted", 0),
                            "skipped": getattr(refreshed, "skipped", 0),
                            "from_date": getattr(refreshed, "from_date", None),
                            "to_date": getattr(refreshed, "to_date", None),
                        }
                    elif command.command_type == COMMAND_RUN_LIVE_CYCLE:
                        result = LiveTradingService(settings).run_cycle(
                            session,
                            trigger=str(payload.get("trigger", "manual")),
                        )
                    else:
                        raise ValueError(f"unsupported worker command: {command.command_type}")
                    runtime.complete_command(session, command, result=result if isinstance(result, dict) else {"result": result})
                except Exception as exc:
                    runtime.fail_command(session, command, str(exc), result={"payload": command.payload_json or {}})
                    logger.exception("[worker] Command %s failed: %s", command.command_type, exc)
    except Exception as exc:
        logger.exception("[worker] command pump failed: %s", exc)


def _start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        _heartbeat_worker,
        "interval",
        seconds=5,
        max_instances=1,
        id="worker_heartbeat",
        replace_existing=True,
    )
    if settings.enable_scheduler:
        sched.add_job(
            _scheduled_ingestion,
            "interval",
            seconds=settings.poll_interval_seconds,
            max_instances=1,
            id="ingestion_tick",
            replace_existing=True,
        )
        if settings.enable_health_audit:
            sched.add_job(
                _scheduled_health_audit,
                "interval",
                seconds=settings.health_check_interval_seconds,
                max_instances=1,
                id="health_audit",
                replace_existing=True,
            )
        if settings.earnings_calendar_auto_refresh:
            sched.add_job(
                _scheduled_earnings_refresh,
                "interval",
                hours=max(1, int(settings.earnings_calendar_refresh_interval_hours)),
                max_instances=1,
                id="earnings_refresh",
                replace_existing=True,
            )
        sched.add_job(
            _process_worker_commands,
            "interval",
            seconds=5,
            max_instances=1,
            id="worker_commands",
            replace_existing=True,
        )
        sched.add_job(
            _scheduled_live_trading,
            "interval",
            seconds=max(60, settings.live_cycle_interval_seconds),
            max_instances=1,
            id="live_cycle",
            replace_existing=True,
        )
        sched.add_job(
            _scheduled_bar_refresh,
            "interval",
            minutes=20,
            max_instances=1,
            id="bar_refresh",
            replace_existing=True,
        )
    sched.start()
    return sched


def _handle_shutdown(signum: int, frame: object | None) -> None:
    logger.info("worker received signal=%s, shutting down", signum)
    _shutdown.set()


def main() -> None:
    global scheduler

    init_db()
    with db_session() as session:
        runtime_control.set_live_enabled(session, settings, runtime_control.get_live_enabled(session, settings), source="worker_boot")
        runtime_control.touch_worker_heartbeat(
            session,
            pid=os.getpid(),
            started_at=_WORKER_STARTED_AT,
            source="worker_boot",
            scheduler_running=False,
            extra={
                "host": _WORKER_HOST,
                "live_enabled": runtime_control.get_live_enabled(session, settings),
                "configured_tickers": market_data.tracked_tickers(),
            },
        )
    logger.info("Worker startup complete")

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    if _is_live_enabled() or settings.agent_mode_enabled:
        threading.Thread(target=_startup_backfill_bars, daemon=True, name="startup-bar-backfill").start()

    scheduler = _start_scheduler()

    if settings.enable_health_audit:
        _scheduled_health_audit()
    if settings.earnings_calendar_auto_refresh:
        _scheduled_earnings_refresh()
    _process_worker_commands()

    logger.info("Worker running: scheduler/background jobs active")
    while not _shutdown.is_set():
        time.sleep(1)

    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
    with db_session() as session:
        runtime_control.mark_worker_offline(session, pid=os.getpid(), source="worker_shutdown", reason="shutdown")


if __name__ == "__main__":
    main()
