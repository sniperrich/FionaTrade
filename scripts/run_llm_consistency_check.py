from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

from sqlalchemy import select

from app.backtest_engine.service import BacktestEngineService
from app.core.config import get_settings
from app.db.database import db_session
from app.db.models import BacktestRun


def _trade_map(trade_log: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in trade_log:
        event_id = row.get("event_id")
        side = row.get("side")
        if event_id is None or not side:
            continue
        out[str(event_id)] = str(side)
    return out


def _agreement_ratio(a: dict[str, str], b: dict[str, str]) -> float:
    keys = sorted(set(a.keys()) | set(b.keys()))
    if not keys:
        return 1.0
    same = sum(1 for k in keys if a.get(k) == b.get(k))
    return same / len(keys)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run repeated LLM backtests and measure signal consistency.")
    parser.add_argument("--start-date", default="2026-01-02")
    parser.add_argument("--end-date", default="2026-01-10")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--min-confidence", type=int, default=70)
    parser.add_argument("--horizon-min", type=int, default=120)
    parser.add_argument("--risk-per-trade-pct", type=float, default=0.001)
    parser.add_argument("--stop-loss-pct", type=float, default=0.015)
    parser.add_argument("--take-profit-pct", type=float, default=0.03)
    parser.add_argument("--slippage-bps", type=float, default=4.0)
    args = parser.parse_args()

    settings = get_settings()
    run_summaries: list[dict] = []

    for i in range(1, max(1, args.runs) + 1):
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
            "risk_per_trade_pct": args.risk_per_trade_pct,
            "stop_loss_pct": args.stop_loss_pct,
            "take_profit_pct": args.take_profit_pct,
            "slippage_bps": args.slippage_bps,
            "model_version": f"{settings.llm_model}|consistency_check_v1",
        }

        with db_session() as session:
            engine = BacktestEngineService(settings)
            result = engine.run(session, params=params)
            run_row = session.execute(select(BacktestRun).where(BacktestRun.id == result.run_id)).scalar_one()
            trade_log = run_row.trade_log or []

        summary = {
            "run_index": i,
            "run_id": result.run_id,
            "trades": int(result.metrics.get("trades", 0)),
            "total_return": float(result.metrics.get("total_return", 0.0)),
            "win_rate": float(result.metrics.get("win_rate", 0.0)),
            "trade_map": _trade_map(trade_log),
        }
        run_summaries.append(summary)
        print(
            f"[consistency] run#{i} run_id={summary['run_id']} "
            f"trades={summary['trades']} return={summary['total_return']:.4f}"
        )

    trades = [x["trades"] for x in run_summaries]
    returns = [x["total_return"] for x in run_summaries]

    pairwise: list[dict] = []
    for i in range(len(run_summaries)):
        for j in range(i + 1, len(run_summaries)):
            a = run_summaries[i]
            b = run_summaries[j]
            agree = _agreement_ratio(a["trade_map"], b["trade_map"])
            pairwise.append(
                {
                    "run_id_a": a["run_id"],
                    "run_id_b": b["run_id"],
                    "trade_direction_agreement": agree,
                }
            )

    avg_agree = mean([x["trade_direction_agreement"] for x in pairwise]) if pairwise else 1.0
    consistency = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start_date": args.start_date, "end_date": args.end_date},
        "llm_model": settings.llm_model,
        "runs": run_summaries,
        "trade_count_mean": mean(trades) if trades else 0.0,
        "trade_count_std": pstdev(trades) if len(trades) > 1 else 0.0,
        "return_mean": mean(returns) if returns else 0.0,
        "return_std": pstdev(returns) if len(returns) > 1 else 0.0,
        "pairwise_agreement": pairwise,
        "pairwise_agreement_mean": avg_agree,
    }

    print(
        f"[consistency] trades mean={consistency['trade_count_mean']:.2f} std={consistency['trade_count_std']:.2f}, "
        f"return mean={consistency['return_mean']:.4f} std={consistency['return_std']:.4f}, "
        f"direction agreement mean={avg_agree:.3f}"
    )

    out_dir = Path(settings.log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"llm_consistency_{ts}.json"
    out_path.write_text(json.dumps(consistency, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[consistency] saved: {out_path}")


if __name__ == "__main__":
    main()
