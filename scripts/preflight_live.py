#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from typing import Any

from app.broker.alpaca import AlpacaBroker
from app.core.config import Settings, get_settings
from app.core.market_hours import market_session_info
from app.db.database import db_session, init_db
from app.monitoring.health import HealthAuditService
from app.services.runtime_control import CONTROL_LIVE_ENABLED, RuntimeControlService
from app.services.worker_runtime import WorkerRuntimeService


def _iso_or_none(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_run_summary(runtime: WorkerRuntimeService, session, run_type: str) -> dict[str, Any] | None:
    row = runtime.latest_run(session, run_type)
    if row is None:
        return None
    return {
        "id": row.id,
        "run_key": row.run_key,
        "status": row.status,
        "stage": row.stage,
        "started_at": _iso_or_none(row.started_at),
        "updated_at": _iso_or_none(row.updated_at),
        "finished_at": _iso_or_none(row.finished_at),
        "error_message": row.error_message,
    }


def build_preflight_snapshot(session, settings: Settings, *, check_broker: bool = False) -> dict[str, Any]:
    runtime = WorkerRuntimeService()
    controls = RuntimeControlService()
    health = HealthAuditService(settings).snapshot(session)
    market = market_session_info()
    live_control = controls.get(session, CONTROL_LIVE_ENABLED) or {}
    latest_live = _latest_run_summary(runtime, session, "live_cycle")
    latest_backfill = _latest_run_summary(runtime, session, "bar_backfill")
    queue = runtime.command_queue_snapshot(session)

    checks: list[dict[str, str]] = []

    def add_check(name: str, level: str, detail: str) -> None:
        checks.append({"name": name, "level": level.upper(), "detail": detail})

    if not health.get("db_ok"):
        add_check("database", "FAIL", "database connectivity failed")
    else:
        add_check("database", "PASS", "database reachable")

    worker = health.get("worker") or {}
    worker_status = str(worker.get("status") or "OFFLINE").upper()
    if worker_status != "ONLINE":
        add_check("worker", "FAIL", f"worker is {worker_status.lower()}")
    else:
        add_check("worker", "PASS", "worker heartbeat is online")

    supervisor = health.get("supervisor") or {}
    supervisor_status = str(supervisor.get("status") or "OFFLINE").upper()
    if supervisor_status != "ONLINE":
        add_check("supervisor", "WARN", f"supervisor is {supervisor_status.lower()}")
    else:
        add_check("supervisor", "PASS", "supervisor heartbeat is online")

    if not settings.llm_base_url or not settings.llm_model:
        add_check("llm", "FAIL", "LLM gateway/model is not configured")
    else:
        add_check("llm", "PASS", f"LLM configured for model {settings.llm_model}")

    if not settings.live_trading_tickers:
        add_check("tickers", "FAIL", "no live trading tickers configured")
    else:
        add_check("tickers", "PASS", f"{len(settings.live_trading_tickers)} live ticker(s) configured")

    if not settings.live_allowed_sources:
        add_check("sources", "WARN", "live source whitelist is empty; code fallback will be used")
    else:
        add_check("sources", "PASS", f"{len(settings.live_allowed_sources)} live source(s) configured")

    source_latency = health.get("source_latency_sec")
    if source_latency is None:
        add_check("ingestion", "WARN", "no source latency available")
    elif float(source_latency) > float(settings.poll_interval_seconds * 3):
        add_check("ingestion", "WARN", f"source latency is high ({source_latency}s)")
    else:
        add_check("ingestion", "PASS", f"source latency {source_latency}s")

    if settings.control_api_key:
        add_check("control_api", "PASS", "control API key configured")
    else:
        add_check("control_api", "WARN", "control API key missing; only localhost bypass protects control plane")

    enabled = bool(live_control.get("enabled", settings.live_trading_enabled))
    if enabled:
        add_check("live_toggle", "PASS", "live trading is enabled")
    else:
        add_check("live_toggle", "WARN", "live trading is disabled")

    if check_broker:
        try:
            broker = AlpacaBroker(settings)
            account = broker.get_account()
            buying_power = float(account.get("buying_power") or 0.0)
            add_check("broker", "PASS", f"broker reachable, buying_power={buying_power:.2f}")
        except Exception as exc:
            add_check("broker", "FAIL", f"broker check failed: {exc}")
    else:
        add_check("broker", "INFO", "broker network check skipped")

    has_fail = any(row["level"] == "FAIL" for row in checks)
    has_warn = any(row["level"] == "WARN" for row in checks)
    verdict = "FAIL" if has_fail else ("WARN" if has_warn else "PASS")

    return {
        "verdict": verdict,
        "checked_at": health.get("checked_at"),
        "market": market,
        "live_control": live_control,
        "health": health,
        "latest_live_cycle": latest_live,
        "latest_bar_backfill": latest_backfill,
        "command_queue": queue,
        "checks": checks,
    }


def _print_human(snapshot: dict[str, Any]) -> None:
    print(f"FionaTrade live preflight: {snapshot['verdict']}")
    print(f"checked_at: {snapshot.get('checked_at')}")
    market = snapshot.get("market") or {}
    print(f"market: {market.get('label')} / tradeable={market.get('tradeable')} / {market.get('et_time_str')}")
    live = snapshot.get("live_control") or {}
    print(f"live_enabled: {bool(live.get('enabled', False))}")
    latest_live = snapshot.get("latest_live_cycle") or {}
    if latest_live:
        print(
            "latest_live_cycle: "
            f"#{latest_live.get('id')} {latest_live.get('status')} stage={latest_live.get('stage')} "
            f"updated_at={latest_live.get('updated_at')}"
        )
    for row in snapshot.get("checks") or []:
        print(f"[{row['level']}] {row['name']}: {row['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local live-trading preflight harness.")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of human-readable text")
    parser.add_argument("--check-broker", action="store_true", help="perform an Alpaca account connectivity check")
    args = parser.parse_args()

    init_db()
    with db_session() as session:
        snapshot = build_preflight_snapshot(session, get_settings(), check_broker=args.check_broker)

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(snapshot)
    return 0 if snapshot["verdict"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
