from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.logging import get_app_logger, setup_logging
from app.db.database import db_session, init_db
from app.services.runtime_control import RuntimeControlService

settings = get_settings()
setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)
logger = get_app_logger()
runtime_control = RuntimeControlService()

_SUPERVISOR_STARTED_AT = datetime.now(timezone.utc)
_HOST = socket.gethostname()
_shutdown = False


def _handle_shutdown(signum: int, frame: object | None) -> None:
    global _shutdown
    logger.info("[supervisor] received signal=%s, shutting down", signum)
    _shutdown = True


def _touch_supervisor(*, child: subprocess.Popen[str] | None, restart_count: int, last_exit_code: int | None, backoff_seconds: float) -> None:
    try:
        with db_session() as session:
            runtime_control.touch_supervisor_heartbeat(
                session,
                pid=os.getpid(),
                started_at=_SUPERVISOR_STARTED_AT,
                source="worker_supervisor",
                scheduler_running=True,
                extra={
                    "host": _HOST,
                    "child_pid": child.pid if child else None,
                    "child_running": bool(child and child.poll() is None),
                    "restart_count": restart_count,
                    "last_exit_code": last_exit_code,
                    "backoff_seconds": backoff_seconds,
                },
            )
    except OperationalError as exc:
        logger.warning("[supervisor] heartbeat skipped due to database lock: %s", exc)


def _spawn_worker() -> subprocess.Popen[str]:
    cmd = [sys.executable, "-m", "app.worker.main"]
    logger.info("[supervisor] starting worker: %s", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        cwd=os.getcwd(),
        text=True,
    )


def _stop_child(child: subprocess.Popen[str] | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("[supervisor] worker did not stop in time; killing pid=%s", child.pid)
        child.kill()
        child.wait(timeout=5)


def main() -> None:
    global _shutdown

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    init_db()
    child: subprocess.Popen[str] | None = None
    restart_count = 0
    last_exit_code: int | None = None
    backoff_seconds = 0.0
    last_heartbeat = 0.0

    try:
        while not _shutdown:
            if child is None or child.poll() is not None:
                if child is not None:
                    last_exit_code = child.returncode
                    logger.warning(
                        "[supervisor] worker exited code=%s, restart_count=%s",
                        last_exit_code,
                        restart_count,
                    )
                    restart_count += 1
                    backoff_seconds = min(60.0, max(2.0, 2.0 ** min(restart_count, 5)))
                    wait_until = time.time() + backoff_seconds
                    while not _shutdown and time.time() < wait_until:
                        if time.time() - last_heartbeat >= 5.0:
                            _touch_supervisor(
                                child=None,
                                restart_count=restart_count,
                                last_exit_code=last_exit_code,
                                backoff_seconds=backoff_seconds,
                            )
                            last_heartbeat = time.time()
                        time.sleep(1)
                    if _shutdown:
                        break
                child = _spawn_worker()
                backoff_seconds = 0.0

            if time.time() - last_heartbeat >= 5.0:
                _touch_supervisor(
                    child=child,
                    restart_count=restart_count,
                    last_exit_code=last_exit_code,
                    backoff_seconds=backoff_seconds,
                )
                last_heartbeat = time.time()
            time.sleep(1)
    finally:
        _stop_child(child)
        try:
            with db_session() as session:
                runtime_control.mark_supervisor_offline(
                    session,
                    pid=os.getpid(),
                    source="worker_supervisor",
                    reason="shutdown",
                )
        except OperationalError as exc:
            logger.warning("[supervisor] offline mark skipped due to database lock: %s", exc)


if __name__ == "__main__":
    main()
