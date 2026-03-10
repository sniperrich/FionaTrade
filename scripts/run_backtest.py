#!/usr/bin/env python
"""一键本地回测脚本 + 自动 acceptance audit

用法:
    python scripts/run_backtest.py                          # 默认：最近一周
    python scripts/run_backtest.py --start 2025-10-01 --end 2025-10-09
    python scripts/run_backtest.py --start 2026-01-06 --end 2026-01-13
    python scripts/run_backtest.py --min-severity 70 --min-confidence 30
    python scripts/run_backtest.py --no-validation          # 关 signal validation
    python scripts/run_backtest.py --slippage 0             # 无摩擦诊断
    python scripts/run_backtest.py --top 20                 # audit 显示 top 20 亏损

结束后自动打印 acceptance audit 报告。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session
from app.backtest_engine.service import BacktestEngineService


def _default_dates() -> tuple[str, str]:
    """默认跑最近 7 天（以今天为 end，往前推 7 天为 start）。"""
    today = datetime.now(tz=timezone.utc).date()
    end = today.isoformat()
    start = (today - timedelta(days=7)).isoformat()
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(description="FionaTrade 一键回测 + acceptance audit")
    parser.add_argument("--start", default=None, help="回测开始日期 YYYY-MM-DD（默认：7天前）")
    parser.add_argument("--end", default=None, help="回测结束日期 YYYY-MM-DD（默认：今天）")
    parser.add_argument("--min-confidence", type=int, default=30, help="最低 confidence 阈值（默认 30）")
    parser.add_argument("--min-severity", type=int, default=70, help="最低 severity 阈值（默认 70）")
    parser.add_argument("--slippage", type=float, default=4.0, help="滑点 bps（默认 4；设 0 做无摩擦诊断）")
    parser.add_argument("--workers", type=int, default=8, help="并发 LLM workers（默认 8）")
    parser.add_argument("--no-validation", action="store_true", help="关闭 signal validation layer")
    parser.add_argument("--no-hard-stops", action="store_true", help="关闭硬止损/止盈")
    parser.add_argument("--no-risk-sizing", action="store_true", help="关闭动态风险仓位")
    parser.add_argument("--top", type=int, default=15, help="audit 显示 top N 亏损交易（默认 15）")
    parser.add_argument("--no-audit", action="store_true", help="跳过 acceptance audit")
    args = parser.parse_args()

    default_start, default_end = _default_dates()
    start_date = args.start or default_start
    end_date = args.end or default_end

    settings = get_settings()
    print(f"\n{'='*60}")
    print(f"  FionaTrade 回测")
    print(f"  模型: {settings.llm_model}")
    print(f"  区间: {start_date} ~ {end_date}")
    print(f"  min_confidence={args.min_confidence}  min_severity={args.min_severity}")
    print(f"  slippage={args.slippage}bps  workers={args.workers}")
    print(f"  validation={'OFF' if args.no_validation else 'ON'}")
    print(f"{'='*60}\n")

    params = {
        "start_date": start_date,
        "end_date": end_date,
        "min_confidence": args.min_confidence,
        "min_severity": args.min_severity,
        "use_llm": True,
        "use_signal_horizon": True,
        "hard_stops": not args.no_hard_stops,
        "risk_sizing": not args.no_risk_sizing,
        "daily_circuit_breaker": True,
        "slippage_bps": args.slippage,
        "use_signal_validation": not args.no_validation,
        "llm_workers": args.workers,
    }

    with db_session() as session:
        svc = BacktestEngineService(settings)
        result = svc.run(session, params=params)

    m = result.metrics
    run_id = result.run_id

    print(f"\n{'='*60}")
    print(f"  回测完成  run_id={run_id}  status={result.status}")
    print(f"{'='*60}")
    print(f"  Events considered : {m.get('events_considered', '?')}")
    print(f"  LLM signals       : {m.get('llm_signals', 0)}")
    print(f"  LLM fallback      : {m.get('llm_fallback_signals', 0)}")
    print(f"  Validation blocked: {m.get('validation_blocked', 0)}")
    print(f"  Trades            : {m.get('trades', 0)}")
    print(f"  Win rate          : {m.get('win_rate', 0)*100:.1f}%")
    print(f"  Total return      : {m.get('total_return', 0)*100:.4f}%")
    print(f"  Net PnL           : ${m.get('total_pnl', 0):.2f}")
    print(f"  Max drawdown      : {m.get('max_drawdown', 0)*100:.3f}%")
    print(f"  Sharpe            : {m.get('sharpe', 0):.3f}")
    print()

    if args.no_audit or m.get("trades", 0) == 0:
        if m.get("trades", 0) == 0:
            print("  (无交易，跳过 audit)")
        return

    # ── Acceptance audit ─────────────────────────────────────────────────────
    print("运行 acceptance audit...\n")
    audit_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "audit_backtest_losses.py"
    )
    os.execv(sys.executable, [sys.executable, audit_script, "--run-id", str(run_id), "--top", str(args.top)])


if __name__ == "__main__":
    main()
