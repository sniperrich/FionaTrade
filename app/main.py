from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.logging import get_app_logger, log_writeout, setup_logging
from app.db.database import db_session, init_db
from app.monitoring.health import HealthAuditService
from app.services.earnings_calendar import EarningsCalendarService
from app.services.live_trading import LiveTradingService
from app.services.orchestrator import PipelineOrchestrator
from app.webui.routes import router as web_router

settings = get_settings()
setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)
logger = get_app_logger()
app = FastAPI(title=settings.app_name)
app.include_router(api_router)
app.include_router(web_router)

scheduler: BackgroundScheduler | None = None


def _scheduled_tick() -> None:
    try:
        with db_session() as session:
            orchestrator = PipelineOrchestrator(settings)
            ingest_result = orchestrator.run_ingestion_validation(session)
            signal_result = orchestrator.run_signals(session)
            paper_result = orchestrator.run_paper_execution(session)
            log_writeout(
                "pipeline_tick",
                {
                    "ingestion": ingest_result,
                    "signals": signal_result,
                    "paper": paper_result,
                },
            )
            logger.info(
                "轮询完成 ingest=%s signals=%s paper=%s",
                ingest_result,
                signal_result,
                paper_result,
            )
    except Exception as exc:
        logger.exception("轮询任务失败: %s", exc)


def _scheduled_health_audit() -> None:
    try:
        with db_session() as session:
            service = HealthAuditService(settings)
            report = service.run_and_log(session, scheduler_running=bool(scheduler and scheduler.running))
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


def _scheduled_live_trading() -> None:
    """Live trading cycle — runs every N seconds during market hours."""
    try:
        with db_session() as session:
            svc = LiveTradingService(settings)
            result = svc.run_cycle(session)
            if result.get("skipped"):
                logger.debug("[live] 模拟盘跳过 reason=%s", result.get("reason"))
            else:
                logger.info(
                    "[live] 模拟盘轮询完成 cycle=%s orders=%s/%s val=$%s",
                    result.get("cycle_id"),
                    result.get("orders_placed", 0),
                    result.get("tickers_processed", 0),
                    result.get("portfolio_value", 0),
                )
    except Exception as exc:
        logger.exception("[live] 模拟盘轮询失败: %s", exc)


@app.on_event("startup")
def startup_event() -> None:
    global scheduler

    init_db()
    if settings.enable_scheduler:
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(_scheduled_tick, "interval", seconds=settings.poll_interval_seconds, max_instances=1)
        if settings.enable_health_audit:
            scheduler.add_job(
                _scheduled_health_audit,
                "interval",
                seconds=settings.health_check_interval_seconds,
                max_instances=1,
            )
        if settings.earnings_calendar_auto_refresh:
            scheduler.add_job(
                _scheduled_earnings_refresh,
                "interval",
                hours=max(1, int(settings.earnings_calendar_refresh_interval_hours)),
                max_instances=1,
            )
        if settings.live_trading_enabled:
            scheduler.add_job(
                _scheduled_live_trading,
                "interval",
                seconds=max(60, settings.live_cycle_interval_seconds),
                max_instances=1,
            )
            logger.info(
                "[live] 模拟盘已启用 interval=%ss tickers=%s",
                settings.live_cycle_interval_seconds,
                settings.live_trading_tickers or settings.agent_tickers_override,
            )
        scheduler.start()
        logger.info(
            "调度器已启动 poll=%ss health_audit=%s/%ss",
            settings.poll_interval_seconds,
            settings.enable_health_audit,
            settings.health_check_interval_seconds,
        )

    if settings.enable_health_audit:
        _scheduled_health_audit()
    if settings.earnings_calendar_auto_refresh:
        _scheduled_earnings_refresh()

    logger.info("中文提示：打开 WebUI http://127.0.0.1:8000 ，健康检查 http://127.0.0.1:8000/api/health")


@app.on_event("shutdown")
def shutdown_event() -> None:
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
