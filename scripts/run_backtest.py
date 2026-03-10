#!/usr/bin/env python
"""FionaTrade 一键回测 + acceptance audit

直接编辑下方 CONFIG 区域修改参数，然后运行：
    python scripts/run_backtest.py
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — 直接在这里改，不需要命令行参数
# ══════════════════════════════════════════════════════════════════════════════

START_DATE       = "2025-10-01"   # 回测开始日期 (YYYY-MM-DD)；None = 7天前
END_DATE         = "2025-10-09"   # 回测结束日期 (YYYY-MM-DD)；None = 今天

MIN_CONFIDENCE   = 30             # 事件 confidence 下限
MIN_SEVERITY     = 70             # 事件 severity 下限（70 = 中强事件）
SLIPPAGE_BPS     = 4.0            # 滑点（bps）；设 0 做无摩擦诊断
LLM_WORKERS      = 8              # 并发 LLM workers

HARD_STOPS       = True           # 硬止损/止盈
RISK_SIZING      = True           # 动态风险仓位
USE_VALIDATION   = True           # Signal Validation Layer
ENTRY_WINDOW_MIN = 120            # 事件后允许进场窗口（分钟）
REGIME_RISK_ADJUST = True         # 按 SPY regime 调节 risk_per_trade_pct
DEDUP_SAME_DAY_EVENT = True       # 同 ticker+同日+同事件类型只保留最高 severity

AUDIT_TOP_N      = 15             # acceptance audit 显示 top N 亏损

# ══════════════════════════════════════════════════════════════════════════════

import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.database import db_session
from app.backtest_engine.service import BacktestEngineService


# ── ANSI helpers ──────────────────────────────────────────────────────────────
def _cyan(s: str) -> str:   return f"\033[96m{s}\033[0m"
def _green(s: str) -> str:  return f"\033[92m{s}\033[0m"
def _red(s: str) -> str:    return f"\033[91m{s}\033[0m"
def _bold(s: str) -> str:   return f"\033[1m{s}\033[0m"
def _dim(s: str) -> str:    return f"\033[2m{s}\033[0m"


# ── Log tailer (real-time progress) ───────────────────────────────────────────
_PROGRESS_RE = re.compile(
    r"回测进度 run_id=(\d+) ([\d.]+)%\((\d+)/(\d+)\) "
    r"trades=(\d+) llm_signals=(\d+) fallback=(\d+) equity=([\d.]+)"
)
_PREFETCH_RE = re.compile(r"并发LLM预取 run_id=(\d+) workers=\d+ tradeable=(\d+)/(\d+)")
_CACHE_RE    = re.compile(r"Finnhub cache预热 (\d+)/(\d+)")
_DONE_RE     = re.compile(r"回测完成 run_id=(\d+)")

_stop_tailer = threading.Event()


def _bar(pct: float, width: int = 30) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _tail_log(log_path: str, run_id_holder: list) -> None:
    """背景线程：实时读 app.log 并渲染进度条到终端。"""
    try:
        with open(log_path, "r") as f:
            f.seek(0, 2)  # jump to end
            phase = "warmup"
            tradeable = 0
            while not _stop_tailer.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.1)
                    continue

                # Finnhub cache warmup
                m = _CACHE_RE.search(line)
                if m:
                    done, total = int(m.group(1)), int(m.group(2))
                    pct = done / total * 100
                    sys.stdout.write(
                        f"\r  {_dim('预热Finnhub cache')}  {_bar(pct, 20)}  {done}/{total}  "
                    )
                    sys.stdout.flush()
                    continue

                # LLM prefetch started
                m = _PREFETCH_RE.search(line)
                if m:
                    tradeable = int(m.group(2))
                    phase = "prefetch"
                    sys.stdout.write(f"\r  {_cyan('LLM预取')} {tradeable} 个事件...{' '*30}\n")
                    sys.stdout.flush()
                    continue

                # Trade loop progress
                m = _PROGRESS_RE.search(line)
                if m:
                    rid = int(m.group(1))
                    if run_id_holder and rid != run_id_holder[0]:
                        continue
                    pct   = float(m.group(2))
                    cur   = m.group(3)
                    total = m.group(4)
                    trades = m.group(5)
                    sigs   = m.group(6)
                    fb     = m.group(7)
                    equity = float(m.group(8))
                    ret_pct = (equity - 100000) / 100000 * 100
                    ret_str = (_green if ret_pct >= 0 else _red)(f"{ret_pct:+.3f}%")
                    sys.stdout.write(
                        f"\r  {_bar(pct)} {pct:5.1f}%  "
                        f"事件 {cur}/{total}  交易 {_bold(trades)}  "
                        f"LLM {sigs}(fb:{fb})  净值 {ret_str}  "
                    )
                    sys.stdout.flush()
                    continue

                # Done
                m = _DONE_RE.search(line)
                if m:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    break
    except Exception:
        pass  # log 文件不存在时静默


# ── Dates ─────────────────────────────────────────────────────────────────────
def _resolve_dates() -> tuple[str, str]:
    today = datetime.now(tz=timezone.utc).date()
    start = START_DATE or (today - timedelta(days=7)).isoformat()
    end   = END_DATE   or today.isoformat()
    return start, end


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    start_date, end_date = _resolve_dates()
    settings = get_settings()

    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        settings.log_dir, "app.log",
    )

    print()
    print(_bold("═" * 62))
    print(_bold(f"  FionaTrade 回测"))
    print(f"  模型      : {_cyan(settings.llm_model)}")
    print(f"  区间      : {start_date}  →  {end_date}")
    print(f"  min_conf  : {MIN_CONFIDENCE}   min_sev : {MIN_SEVERITY}")
    print(f"  slippage  : {SLIPPAGE_BPS} bps   workers : {LLM_WORKERS}")
    print(f"  entry_win : {ENTRY_WINDOW_MIN}m  regime_adj: {'ON' if REGIME_RISK_ADJUST else 'OFF'}")
    print(f"  validation: {'ON' if USE_VALIDATION else 'OFF'}   "
          f"hard_stops: {'ON' if HARD_STOPS else 'OFF'}")
    print(_bold("═" * 62))
    print()

    params = {
        "start_date"         : start_date,
        "end_date"           : end_date,
        "min_confidence"     : MIN_CONFIDENCE,
        "min_severity"       : MIN_SEVERITY,
        "use_llm"            : True,
        "use_signal_horizon" : True,
        "hard_stops"         : HARD_STOPS,
        "risk_sizing"        : RISK_SIZING,
        "entry_window_min"   : ENTRY_WINDOW_MIN,
        "regime_risk_adjust" : REGIME_RISK_ADJUST,
        "dedup_same_day_event": DEDUP_SAME_DAY_EVENT,
        "daily_circuit_breaker": True,
        "slippage_bps"       : SLIPPAGE_BPS,
        "use_signal_validation": USE_VALIDATION,
        "llm_workers"        : LLM_WORKERS,
        "progress_every"     : 10,
    }

    # 启动 log tailer（实时进度）
    run_id_holder: list[int] = []
    tailer = threading.Thread(
        target=_tail_log, args=(log_path, run_id_holder), daemon=True
    )
    tailer.start()

    t0 = time.time()
    with db_session() as session:
        svc = BacktestEngineService(settings)
        result = svc.run(session, params=params)

    _stop_tailer.set()
    tailer.join(timeout=2)
    elapsed = time.time() - t0

    m = result.metrics
    run_id = result.run_id
    run_id_holder.append(run_id)

    trades     = m.get("trades", 0)
    win_rate   = m.get("win_rate", 0) * 100
    total_ret  = m.get("total_return", 0) * 100
    pnl        = m.get("total_pnl", 0)
    drawdown   = m.get("max_drawdown", 0) * 100
    sharpe     = m.get("sharpe", 0)

    ret_color  = _green if total_ret >= 0 else _red
    wr_color   = _green if win_rate >= 50 else _red

    print()
    print(_bold("═" * 62))
    print(_bold(f"  回测完成  run_id={run_id}  耗时 {elapsed:.0f}s"))
    print(_bold("═" * 62))
    print(f"  Events considered  : {m.get('events_considered', '?')}")
    print(f"  LLM signals        : {m.get('llm_signals', 0)}  "
          f"fallback: {m.get('llm_fallback_signals', 0)}")
    print(f"  Validation blocked : {m.get('validation_blocked', 0)}")
    print(f"  Trades             : {_bold(str(trades))}")
    print(f"  Win rate           : {wr_color(f'{win_rate:.1f}%')}")
    print(f"  Total return       : {ret_color(f'{total_ret:+.4f}%')}")
    print(f"  Net PnL            : {ret_color(f'${pnl:+.2f}')}")
    print(f"  Max drawdown       : {drawdown:.3f}%")
    print(f"  Sharpe             : {sharpe:.3f}")
    print(_bold("═" * 62))
    print()

    if trades == 0:
        print("  (无交易，跳过 acceptance audit)")
        return

    # ── Acceptance audit ──────────────────────────────────────────────────────
    print(_bold("  ACCEPTANCE AUDIT"))
    print()
    audit_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "audit_backtest_losses.py"
    )
    os.execv(sys.executable, [
        sys.executable, audit_script,
        "--run-id", str(run_id),
        "--top", str(AUDIT_TOP_N),
    ])


if __name__ == "__main__":
    main()
