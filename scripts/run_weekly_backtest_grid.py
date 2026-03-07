from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

from app.backtest_engine.service import BacktestEngineService
from app.core.config import get_settings
from app.db.database import db_session


def _parse_float_grid(raw: str, default: list[float]) -> list[float]:
    if not raw.strip():
        return default
    values: list[float] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(float(item))
    return values or default


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one-week LLM backtest grid with min-trades filtering.")
    parser.add_argument("--start-date", default="2026-01-02", help="Inclusive start date, e.g. 2026-01-02")
    parser.add_argument("--end-date", default="2026-01-10", help="Exclusive end date, e.g. 2026-01-10")
    parser.add_argument("--min-confidence", type=int, default=70)
    parser.add_argument("--horizon-min", type=int, default=120)
    parser.add_argument("--min-trades", type=int, default=5)
    parser.add_argument("--risk-grid", default="0.0005,0.001,0.0015")
    parser.add_argument("--stop-grid", default="0.01,0.015,0.02")
    parser.add_argument("--take-grid", default="0.02,0.03,0.04")
    parser.add_argument("--slippage-bps", type=float, default=0.0, help="Backtest slippage in bps, default 0 for friction test.")
    parser.add_argument("--model-version", default="", help="Optional model/prompt version tag for comparison.")
    args = parser.parse_args()

    risk_grid = _parse_float_grid(args.risk_grid, [0.0005, 0.001, 0.0015])
    stop_grid = _parse_float_grid(args.stop_grid, [0.01, 0.015, 0.02])
    take_grid = _parse_float_grid(args.take_grid, [0.02, 0.03, 0.04])
    combos = list(product(risk_grid, stop_grid, take_grid))

    settings = get_settings()
    model_version = args.model_version.strip() or f"{settings.llm_model}|direction_v3_quant_fulltext"
    engine = BacktestEngineService(settings)

    print(
        f"[grid] start one-week LLM grid: {args.start_date} -> {args.end_date}, "
        f"combos={len(combos)}, min_trades={args.min_trades}, model={model_version}"
    )

    rows: list[dict] = []
    for idx, (risk, stop, take) in enumerate(combos, start=1):
        params = {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "use_llm": True,
            "use_signal_horizon": True,
            "enable_term_horizon": False,
            "hard_stops": True,
            "risk_sizing": True,
            "daily_circuit_breaker": True,
            "min_confidence": args.min_confidence,
            "horizon_min": args.horizon_min,
            "risk_per_trade_pct": risk,
            "stop_loss_pct": stop,
            "take_profit_pct": take,
            "slippage_bps": args.slippage_bps,
            "model_version": model_version,
        }

        with db_session() as session:
            result = engine.run(session, params=params)
            metrics = result.metrics or {}

        row = {
            "run_id": result.run_id,
            "risk_per_trade_pct": risk,
            "stop_loss_pct": stop,
            "take_profit_pct": take,
            "trades": int(metrics.get("trades", 0)),
            "total_return": float(metrics.get("total_return", 0.0)),
            "max_drawdown": float(metrics.get("max_drawdown", 0.0)),
            "win_rate": float(metrics.get("win_rate", 0.0)),
            "profit_factor": float(metrics.get("profit_factor", 0.0)),
            "llm_signals": int(metrics.get("llm_signals", 0)),
            "llm_fallback_signals": int(metrics.get("llm_fallback_signals", 0)),
            "model_version": model_version,
            "slippage_bps": args.slippage_bps,
            "passes_min_trades": int(metrics.get("trades", 0)) >= args.min_trades,
        }
        rows.append(row)
        print(
            f"[grid] {idx:02d}/{len(combos)} run_id={row['run_id']} "
            f"risk={risk:.4f} stop={stop:.3f} take={take:.3f} "
            f"trades={row['trades']} ret={row['total_return']:.4f}"
        )

    eligible = [row for row in rows if row["passes_min_trades"]]
    best = max(eligible, key=lambda r: r["total_return"]) if eligible else None
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week_window": {"start_date": args.start_date, "end_date": args.end_date},
        "model_version": model_version,
        "min_trades": args.min_trades,
        "combos": len(combos),
        "eligible_count": len(eligible),
        "best_eligible": best,
        "results": rows,
    }

    out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"weekly_grid_{ts}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if best:
        print(
            f"[grid] best eligible run_id={best['run_id']} trades={best['trades']} "
            f"ret={best['total_return']:.4f} risk={best['risk_per_trade_pct']:.4f} "
            f"stop={best['stop_loss_pct']:.3f} take={best['take_profit_pct']:.3f}"
        )
    else:
        print("[grid] no combo reached min_trades constraint; check prompt/model or lower --min-trades.")
    print(f"[grid] saved summary: {out_path}")


if __name__ == "__main__":
    main()
