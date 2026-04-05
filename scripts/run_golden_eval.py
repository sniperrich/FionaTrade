#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.backtest_engine.service import BacktestEngineService
from app.core.config import get_settings
from app.db.database import db_session, init_db

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "tests" / "golden" / "manifest.json"


def load_manifest(path: Path = MANIFEST_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("golden manifest must contain a top-level 'cases' list")
    return [dict(case) for case in cases]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    params = dict(case.get("params") or {})
    with db_session() as session:
        result = BacktestEngineService(get_settings()).run(session, params=params)
    metrics = dict(result.metrics or {})
    return {
        "id": case.get("id"),
        "label": case.get("label"),
        "status": result.status,
        "run_id": result.run_id,
        "engine_mode": metrics.get("engine_mode") or params.get("engine_mode") or "event",
        "trades": metrics.get("trades"),
        "total_return": metrics.get("total_return"),
        "win_rate": metrics.get("win_rate"),
        "max_drawdown": metrics.get("max_drawdown"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FionaTrade golden evaluation cases from tests/golden/manifest.json")
    parser.add_argument("--case", action="append", default=[], help="specific case id(s) to run")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()

    init_db()
    cases = load_manifest()
    selected_ids = {str(item).strip() for item in args.case if str(item).strip()}
    if selected_ids:
        cases = [case for case in cases if str(case.get("id")) in selected_ids]
    if not cases:
        raise SystemExit("no golden cases selected")

    results = [run_case(case) for case in cases]
    if args.json:
        print(json.dumps({"manifest": str(MANIFEST_PATH), "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"FionaTrade golden eval: {len(results)} case(s)")
        for row in results:
            print(
                f"- {row['id']}: {row['label']} | {row['engine_mode']} | "
                f"trades={row['trades']} return={row['total_return']} win_rate={row['win_rate']} drawdown={row['max_drawdown']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
