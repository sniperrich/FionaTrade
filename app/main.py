from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.logging import get_app_logger, setup_logging
from app.db.database import init_db
from app.webui.routes import router as web_router

settings = get_settings()
setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)
logger = get_app_logger()

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.on_event("startup")
def startup_event() -> None:
    init_db()
    host = os.getenv("FIONA_WEB_HOST", "127.0.0.1")
    port = os.getenv("FIONA_WEB_PORT", "6888")
    logger.info("Web startup complete")
    logger.info("中文提示：打开 WebUI http://%s:%s ，健康检查 http://%s:%s/api/health", host, port, host, port)
    logger.info("中文提示：后台任务已迁移到 worker 进程，推荐单独运行 python -m app.worker.supervisor")
