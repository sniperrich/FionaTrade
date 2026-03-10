"""Backtest Loss Acceptance Audit
================================
Analyzes a completed backtest run and produces a structured acceptance report:

  - Overall performance summary
  - Loss breakdown by event type, ticker, source, exit reason
  - Top losing trades with original article titles and event summary
  - Signal quality breakdown (LLM vs fallback, validation pass/block)
  - Actionable diagnosis: what event types / sources should be filtered

Usage
-----
    python scripts/audit_backtest_losses.py                     # latest run
    python scripts/audit_backtest_losses.py --run-id 41        # specific run
    python scripts/audit_backtest_losses.py --run-id 41 --top 20
    python scripts/audit_backtest_losses.py --run-id 41 --format json

Outputs to stdout.  Use  | tee report.txt  to save.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── ensure project root is on sys.path ───────────────────────────────────────
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text

from app.db.database import SessionLocal
from app.db.models import BacktestRun, BacktestTrade, Event, EventEvidence, RawItem


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TradeDetail:
    trade_id: int
    run_id: int
    ticker: str
    side: str
    pnl: float
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: float
    event_type: str
    event_id: int | None
    # enriched from event / evidence
    event_summary: str = ""
    evidence_titles: list[str] = field(default_factory=list)
    evidence_sources: list[str] = field(default_factory=list)
    evidence_urls: list[str] = field(default_factory=list)
    event_time: str = ""
    event_confidence: int = 0
    event_severity: int = 0


@dataclass
class AttributionGroup:
    key: str
    total_pnl: float
    trades: int
    wins: int
    losses: int
    pnl_share_pct: float = 0.0   # share of total gross loss (negative = blame)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.trades if self.trades else 0.0


@dataclass
class AcceptanceReport:
    run_id: int
    created_at: str
    params: dict
    metrics: dict

    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    gross_loss: float
    gross_profit: float
    win_rate: float
    total_return_pct: float

    by_event_type: list[AttributionGroup]
    by_ticker: list[AttributionGroup]
    by_source: list[AttributionGroup]
    by_exit_reason: list[AttributionGroup]

    top_losers: list[TradeDetail]
    top_winners: list[TradeDetail]

    diagnosis: list[str]


# ── Main audit logic ──────────────────────────────────────────────────────────

def load_run(session, run_id: int | None) -> BacktestRun:
    if run_id is not None:
        run = session.get(BacktestRun, run_id)
        if run is None:
            print(f"ERROR: run_id={run_id} not found.", file=sys.stderr)
            sys.exit(1)
        return run
    # latest
    run = session.execute(
        select(BacktestRun).where(BacktestRun.status == "DONE").order_by(BacktestRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        print("ERROR: No completed backtest runs found.", file=sys.stderr)
        sys.exit(1)
    return run


def load_trades(session, run_id: int) -> list[BacktestTrade]:
    return (
        session.execute(
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run_id)
            .order_by(BacktestTrade.pnl.asc())
        )
        .scalars()
        .all()
    )


def enrich_trade(session, trade: BacktestTrade) -> TradeDetail:
    """Pull event summary + all article titles for a single trade."""
    detail = TradeDetail(
        trade_id=trade.id,
        run_id=trade.run_id,
        ticker=trade.ticker,
        side=trade.side,
        pnl=trade.pnl,
        entry_ts=trade.entry_ts.isoformat() if trade.entry_ts else "",
        exit_ts=trade.exit_ts.isoformat() if trade.exit_ts else "",
        entry_price=float(trade.entry_price or 0),
        exit_price=float(trade.exit_price or 0),
        qty=float(trade.qty or 0),
        event_type=trade.event_type or "",
        event_id=None,
    )

    # The trade_log on BacktestRun has event_id; BacktestTrade doesn't store it directly.
    # Resolve via BacktestRun.trade_log using the trade's entry_ts + ticker as key.
    run = session.get(BacktestRun, trade.run_id)
    event_id: int | None = None
    if run and run.trade_log:
        entry_ts_str = detail.entry_ts
        for log_entry in run.trade_log:
            if (
                log_entry.get("ticker") == trade.ticker
                and log_entry.get("entry_ts", "").startswith(entry_ts_str[:16])
                and abs(float(log_entry.get("pnl", 0)) - trade.pnl) < 0.01
            ):
                event_id = log_entry.get("event_id")
                break
    detail.event_id = event_id

    if event_id:
        event = session.get(Event, event_id)
        if event:
            detail.event_summary = event.summary or ""
            detail.event_time = event.event_time.isoformat() if event.event_time else ""
            detail.event_confidence = event.confidence or 0
            detail.event_severity = event.severity or 0

        # Pull evidence (article titles + sources)
        evidences = (
            session.execute(
                select(EventEvidence, RawItem)
                .join(RawItem, RawItem.id == EventEvidence.raw_item_id, isouter=True)
                .where(EventEvidence.event_id == event_id)
                .order_by(EventEvidence.source_tier.asc(), EventEvidence.id.asc())
                .limit(5)
            )
            .all()
        )
        for ev, raw in evidences:
            title = (raw.title if raw else None) or ev.summary or ""
            detail.evidence_titles.append(title[:200])
            detail.evidence_sources.append(ev.source or "")
            detail.evidence_urls.append(ev.url or "")

    return detail


def build_attribution(
    trades: list[BacktestTrade],
    key_fn,
    gross_loss: float,
) -> list[AttributionGroup]:
    groups: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for t in trades:
        k = key_fn(t) or "unknown"
        groups[k]["pnl"] += t.pnl
        groups[k]["trades"] += 1
        if t.pnl >= 0:
            groups[k]["wins"] += 1
        else:
            groups[k]["losses"] += 1

    result = []
    for key, g in groups.items():
        share = (g["pnl"] / gross_loss * 100.0) if gross_loss < 0 and g["pnl"] < 0 else 0.0
        result.append(AttributionGroup(
            key=key,
            total_pnl=g["pnl"],
            trades=g["trades"],
            wins=g["wins"],
            losses=g["losses"],
            pnl_share_pct=share,
        ))
    # Sort: losers first (most negative pnl first), then winners
    result.sort(key=lambda x: x.total_pnl)
    return result


def source_key_from_run(run: BacktestRun, trade: BacktestTrade) -> str:
    """Resolve source from trade log (BacktestTrade doesn't store source directly)."""
    if not run or not run.trade_log:
        return "unknown"
    for log_entry in run.trade_log:
        if (
            log_entry.get("ticker") == trade.ticker
            and abs(float(log_entry.get("pnl", 0)) - trade.pnl) < 0.01
        ):
            return log_entry.get("source", "unknown")
    return "unknown"


def exit_reason_from_run(run: BacktestRun, trade: BacktestTrade) -> str:
    if not run or not run.trade_log:
        return "HORIZON"
    for log_entry in run.trade_log:
        if (
            log_entry.get("ticker") == trade.ticker
            and abs(float(log_entry.get("pnl", 0)) - trade.pnl) < 0.01
        ):
            return log_entry.get("exit_reason", "HORIZON")
    return "HORIZON"


def build_diagnosis(
    by_event_type: list[AttributionGroup],
    by_ticker: list[AttributionGroup],
    by_source: list[AttributionGroup],
    top_losers: list[TradeDetail],
    metrics: dict,
) -> list[str]:
    diag: list[str] = []

    # 1. Worst event type
    losing_types = [g for g in by_event_type if g.total_pnl < 0]
    if losing_types:
        worst = losing_types[0]
        diag.append(
            f"WORST EVENT TYPE: '{worst.key}' accounts for ${abs(worst.total_pnl):.0f} gross loss "
            f"({worst.pnl_share_pct:.1f}% of total losses), win_rate={worst.win_rate:.0%}, "
            f"{worst.losses} losing trades."
        )
        if worst.win_rate < 0.35:
            diag.append(
                f"  → '{worst.key}' win rate {worst.win_rate:.0%} is below 35%. "
                "Consider adding to EXCLUDED_FROM_TRADING or requiring higher confidence."
            )

    # 2. Consistent losers by ticker
    bad_tickers = [g for g in by_ticker if g.total_pnl < 0 and g.losses >= 3]
    if bad_tickers:
        t = bad_tickers[0]
        diag.append(
            f"WORST TICKER: '{t.key}' lost ${abs(t.total_pnl):.0f} over {t.losses} losing trades "
            f"(win_rate={t.win_rate:.0%}). Check if events are correctly tagged to this ticker."
        )

    # 3. Fallback signal quality
    fallback_ratio = metrics.get("llm_fallback_signals", 0) / max(metrics.get("llm_signals", 1), 1)
    if fallback_ratio > 0.2:
        diag.append(
            f"HIGH FALLBACK RATE: {fallback_ratio:.0%} of LLM signals used rule fallback. "
            "LLM may be failing frequently — check gateway connectivity."
        )

    # 4. Priced-in pattern (trades where we entered late — large move already happened)
    large_entry_moves = [
        td for td in top_losers
        if td.entry_price > 0 and td.event_time
        and abs(td.entry_price - float(td.entry_price)) / max(td.entry_price, 1) < 0.001
    ]

    # 5. Evidence quality pattern
    no_title_count = sum(1 for td in top_losers if not td.evidence_titles)
    if no_title_count > len(top_losers) * 0.3:
        diag.append(
            f"MISSING EVIDENCE: {no_title_count}/{len(top_losers)} top losers have no article "
            "titles. Events may be getting tagged without real directional content."
        )

    # 6. Ticker mismatch pattern (article mentions ticker but isn't about that ticker)
    generic_titles = [
        td for td in top_losers
        if td.evidence_titles and any(
            kw in " ".join(td.evidence_titles).lower()
            for kw in ["trending", "movers", "market summary", "market today", "round-up",
                       "look past", "shutdown", "equity futures", "dow jones"]
        )
    ]
    if generic_titles:
        diag.append(
            f"GENERIC ARTICLES: {len(generic_titles)}/{len(top_losers)} top losers have "
            "market-round-up headlines with no ticker-specific signal. "
            "Consider stricter title/body quality filters in normalization."
        )

    # 7. Validation blocked count
    val_blocked = metrics.get("validation_blocked", 0)
    if val_blocked > 0:
        diag.append(
            f"VALIDATION GATE: blocked {val_blocked} signals before execution "
            "(these are NOT in the trade count — validation is working)."
        )

    # 8. Stop-loss dominance check
    exit_reasons = metrics.get("exit_reason_counts", {})
    horizon_exits = exit_reasons.get("HORIZON", 0)
    stop_exits = exit_reasons.get("STOP", 0)
    take_exits = exit_reasons.get("TAKE", 0)
    total_exits = horizon_exits + stop_exits + take_exits
    if total_exits > 0 and stop_exits / total_exits > 0.15:
        diag.append(
            f"STOP-LOSS RATE: {stop_exits}/{total_exits} trades ({stop_exits/total_exits:.0%}) "
            "exited via stop-loss. Directional accuracy may be worse than win_rate suggests."
        )

    if not diag:
        diag.append("No critical issues detected. Signal quality looks acceptable.")

    return diag


# ── Formatting ────────────────────────────────────────────────────────────────

SEP = "─" * 80


def fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def print_report(report: AcceptanceReport, top_n: int = 15) -> None:
    m = report.metrics
    params = report.params

    print()
    print("=" * 80)
    print(f"  BACKTEST ACCEPTANCE REPORT  ·  run_id={report.run_id}")
    print(f"  Created: {report.created_at}")
    print("=" * 80)

    # ── params ────────────────────────────────────────────────────────────────
    print(f"\n{'PARAMS':}")
    print(SEP)
    key_params = ["start_date", "end_date", "min_confidence", "min_severity",
                  "use_llm", "slippage_bps", "stop_loss_pct", "take_profit_pct",
                  "use_signal_validation", "validation_min_review_score"]
    for k in key_params:
        if k in params:
            print(f"  {k:<35} {params[k]}")

    # ── overall performance ───────────────────────────────────────────────────
    print(f"\n{'OVERALL PERFORMANCE':}")
    print(SEP)
    print(f"  Total trades          : {report.total_trades}")
    print(f"  Winners / Losers      : {report.winning_trades} / {report.losing_trades}")
    print(f"  Win rate              : {report.win_rate:.1%}")
    print(f"  Total return          : {report.total_return_pct:+.4f}%")
    print(f"  Gross profit          : {fmt_pnl(report.gross_profit)}")
    print(f"  Gross loss            : {fmt_pnl(report.gross_loss)}")
    print(f"  Net PnL               : {fmt_pnl(report.total_pnl)}")
    print(f"  Profit factor         : {m.get('profit_factor', 0):.3f}")
    print(f"  Sharpe                : {m.get('sharpe', 0):.3f}")
    print(f"  Max drawdown          : {m.get('max_drawdown', 0):.4f}")
    print(f"  Avg win               : {fmt_pnl(m.get('avg_win', 0))}")
    print(f"  Avg loss              : {fmt_pnl(-abs(m.get('avg_loss', 0)))}")
    print(f"  PnL ratio (W/L)       : {m.get('pnl_ratio', 0):.2f}x")
    print(f"  LLM signals           : {m.get('llm_signals', 0)}")
    print(f"  LLM fallback          : {m.get('llm_fallback_signals', 0)}")
    print(f"  Validation blocked    : {m.get('validation_blocked', 0)}")
    exit_cnts = m.get("exit_reason_counts", {})
    for reason, cnt in sorted(exit_cnts.items()):
        print(f"  Exit [{reason:<10}]     : {cnt}")

    # ── event-type attribution ────────────────────────────────────────────────
    print(f"\n{'LOSS ATTRIBUTION BY EVENT TYPE':}")
    print(SEP)
    print(f"  {'Event Type':<30} {'PnL':>10}  {'Trades':>6}  {'Wins':>4}  {'WinRate':>7}  {'Loss%':>6}")
    print(f"  {'-'*30} {'-'*10}  {'-'*6}  {'-'*4}  {'-'*7}  {'-'*6}")
    for g in report.by_event_type:
        share_str = f"{g.pnl_share_pct:.1f}%" if g.total_pnl < 0 else ""
        print(
            f"  {g.key:<30} {fmt_pnl(g.total_pnl):>10}  {g.trades:>6}  {g.wins:>4}  "
            f"{g.win_rate:>7.0%}  {share_str:>6}"
        )

    # ── ticker attribution ────────────────────────────────────────────────────
    print(f"\n{'LOSS ATTRIBUTION BY TICKER (top losers)':}")
    print(SEP)
    print(f"  {'Ticker':<10} {'PnL':>10}  {'Trades':>6}  {'Wins':>4}  {'WinRate':>7}  {'Loss%':>6}")
    print(f"  {'-'*10} {'-'*10}  {'-'*6}  {'-'*4}  {'-'*7}  {'-'*6}")
    for g in report.by_ticker[:20]:
        share_str = f"{g.pnl_share_pct:.1f}%" if g.total_pnl < 0 else ""
        print(
            f"  {g.key:<10} {fmt_pnl(g.total_pnl):>10}  {g.trades:>6}  {g.wins:>4}  "
            f"{g.win_rate:>7.0%}  {share_str:>6}"
        )

    # ── source attribution ────────────────────────────────────────────────────
    if any(g.key != "unknown" for g in report.by_source):
        print(f"\n{'LOSS ATTRIBUTION BY SOURCE':}")
        print(SEP)
        print(f"  {'Source':<20} {'PnL':>10}  {'Trades':>6}  {'WinRate':>7}")
        print(f"  {'-'*20} {'-'*10}  {'-'*6}  {'-'*7}")
        for g in report.by_source[:15]:
            print(
                f"  {g.key:<20} {fmt_pnl(g.total_pnl):>10}  {g.trades:>6}  {g.win_rate:>7.0%}"
            )

    # ── top losing trades with original articles ──────────────────────────────
    print(f"\n{'TOP ' + str(top_n) + ' LOSING TRADES — WITH ORIGINAL ARTICLES':}")
    print(SEP)
    for i, td in enumerate(report.top_losers[:top_n], 1):
        print(f"\n  #{i:02d}  {td.ticker:<6}  {td.side:<5}  PnL={fmt_pnl(td.pnl):<12}  "
              f"type={td.event_type}")
        print(f"       entry={td.entry_ts[:16]}  exit={td.exit_ts[:16]}  "
              f"qty={td.qty:.2f}  entry_px={td.entry_price:.2f}  exit_px={td.exit_price:.2f}")
        if td.event_time:
            print(f"       event_time={td.event_time[:16]}  "
                  f"confidence={td.event_confidence}  severity={td.event_severity}")
        if td.event_summary:
            summary_wrapped = textwrap.fill(td.event_summary, width=72, initial_indent="       Event: ",
                                            subsequent_indent="              ")
            print(summary_wrapped)
        if td.evidence_titles:
            print(f"       Articles ({len(td.evidence_titles)}):")
            for j, (title, src, url) in enumerate(
                zip(td.evidence_titles, td.evidence_sources, td.evidence_urls), 1
            ):
                print(f"         [{j}] [{src}] {title}")
                if url and len(url) < 120:
                    print(f"              {url}")
        else:
            print(f"       Articles: (none found — event may have no evidence linked)")

    # ── top winning trades ────────────────────────────────────────────────────
    print(f"\n{'TOP 5 WINNING TRADES':}")
    print(SEP)
    for i, td in enumerate(report.top_winners[:5], 1):
        print(f"  #{i:02d}  {td.ticker:<6}  {td.side:<5}  PnL={fmt_pnl(td.pnl):<12}  type={td.event_type}")
        if td.event_summary:
            print(f"       Event: {td.event_summary[:120]}")
        if td.evidence_titles:
            print(f"       [{td.evidence_sources[0]}] {td.evidence_titles[0][:120]}")

    # ── diagnosis ─────────────────────────────────────────────────────────────
    print(f"\n{'DIAGNOSIS & RECOMMENDATIONS':}")
    print(SEP)
    for item in report.diagnosis:
        lines = textwrap.wrap(item, width=76)
        for j, line in enumerate(lines):
            print(f"  {'▶' if j == 0 else ' '} {line}")
        print()

    print("=" * 80)
    print(f"  END OF REPORT  ·  run_id={report.run_id}")
    print("=" * 80)
    print()


def print_json(report: AcceptanceReport) -> None:
    out = {
        "run_id": report.run_id,
        "created_at": report.created_at,
        "params": report.params,
        "summary": {
            "total_trades": report.total_trades,
            "winning_trades": report.winning_trades,
            "losing_trades": report.losing_trades,
            "win_rate": report.win_rate,
            "total_return_pct": report.total_return_pct,
            "gross_profit": report.gross_profit,
            "gross_loss": report.gross_loss,
            "net_pnl": report.total_pnl,
            "profit_factor": report.metrics.get("profit_factor", 0),
            "sharpe": report.metrics.get("sharpe", 0),
            "max_drawdown": report.metrics.get("max_drawdown", 0),
        },
        "by_event_type": [
            {
                "key": g.key, "total_pnl": g.total_pnl, "trades": g.trades,
                "wins": g.wins, "losses": g.losses, "win_rate": g.win_rate,
                "loss_share_pct": g.pnl_share_pct,
            }
            for g in report.by_event_type
        ],
        "by_ticker": [
            {
                "key": g.key, "total_pnl": g.total_pnl, "trades": g.trades,
                "wins": g.wins, "losses": g.losses, "win_rate": g.win_rate,
                "loss_share_pct": g.pnl_share_pct,
            }
            for g in report.by_ticker
        ],
        "top_losers": [
            {
                "ticker": td.ticker, "side": td.side, "pnl": td.pnl,
                "event_type": td.event_type, "event_summary": td.event_summary,
                "event_confidence": td.event_confidence, "event_severity": td.event_severity,
                "entry_ts": td.entry_ts, "exit_ts": td.exit_ts,
                "articles": [
                    {"source": s, "title": t, "url": u}
                    for s, t, u in zip(td.evidence_sources, td.evidence_titles, td.evidence_urls)
                ],
            }
            for td in report.top_losers
        ],
        "diagnosis": report.diagnosis,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest loss acceptance audit")
    parser.add_argument("--run-id", type=int, default=None, help="BacktestRun ID (default: latest)")
    parser.add_argument("--top", type=int, default=15, help="How many top losers to show (default: 15)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    with SessionLocal() as session:
        run = load_run(session, args.run_id)
        trades = load_trades(session, run.id)

        if not trades:
            print(f"No trades found for run_id={run.id}.", file=sys.stderr)
            sys.exit(0)

        metrics = run.metrics or {}
        params = run.params or {}

        # ── Basic PnL stats ───────────────────────────────────────────────────
        pnl_list = [t.pnl for t in trades]
        winning = [p for p in pnl_list if p > 0]
        losing = [p for p in pnl_list if p <= 0]
        gross_profit = sum(winning)
        gross_loss = sum(losing)
        total_pnl = sum(pnl_list)
        win_rate = len(winning) / len(pnl_list) if pnl_list else 0.0
        total_return_pct = metrics.get("total_return", 0.0) * 100.0

        # ── Attribution groups ────────────────────────────────────────────────
        by_event_type = build_attribution(trades, lambda t: t.event_type, gross_loss)
        by_ticker = build_attribution(trades, lambda t: t.ticker, gross_loss)

        # Source attribution from run.trade_log
        _log_by_key: dict[tuple, str] = {}
        if run.trade_log:
            for entry in run.trade_log:
                k = (entry.get("ticker"), round(float(entry.get("pnl", 0)), 2))
                _log_by_key[k] = entry.get("source", "unknown")

        def _source_key(t: BacktestTrade) -> str:
            k = (t.ticker, round(t.pnl, 2))
            return _log_by_key.get(k, "unknown")

        # Use existing source_attribution from metrics if available
        by_source: list[AttributionGroup] = []
        if metrics.get("source_attribution"):
            for src, pnl_val in sorted(metrics["source_attribution"].items(), key=lambda x: x[1]):
                src_trades = [t for t in trades if _source_key(t) == src]
                src_wins = [t for t in src_trades if t.pnl > 0]
                share = (pnl_val / gross_loss * 100.0) if gross_loss < 0 and pnl_val < 0 else 0.0
                by_source.append(AttributionGroup(
                    key=src,
                    total_pnl=pnl_val,
                    trades=len(src_trades),
                    wins=len(src_wins),
                    losses=len(src_trades) - len(src_wins),
                    pnl_share_pct=share,
                ))
            by_source.sort(key=lambda x: x.total_pnl)

        by_exit_reason: list[AttributionGroup] = []

        # ── Enrich top N losers ───────────────────────────────────────────────
        sorted_losers = sorted(trades, key=lambda t: t.pnl)
        sorted_winners = sorted(trades, key=lambda t: t.pnl, reverse=True)

        print(f"Enriching top {args.top} losing trades (fetching article titles)...",
              file=sys.stderr)
        top_losers: list[TradeDetail] = []
        for trade in sorted_losers[:args.top]:
            top_losers.append(enrich_trade(session, trade))

        top_winners: list[TradeDetail] = []
        for trade in sorted_winners[:5]:
            top_winners.append(enrich_trade(session, trade))

        diagnosis = build_diagnosis(by_event_type, by_ticker, by_source, top_losers, metrics)

        report = AcceptanceReport(
            run_id=run.id,
            created_at=run.created_at.isoformat() if run.created_at else "",
            params=params,
            metrics=metrics,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            total_pnl=total_pnl,
            gross_loss=gross_loss,
            gross_profit=gross_profit,
            win_rate=win_rate,
            total_return_pct=total_return_pct,
            by_event_type=by_event_type,
            by_ticker=by_ticker,
            by_source=by_source,
            by_exit_reason=by_exit_reason,
            top_losers=top_losers,
            top_winners=top_winners,
            diagnosis=diagnosis,
        )

    if args.format == "json":
        print_json(report)
    else:
        print_report(report, top_n=args.top)


if __name__ == "__main__":
    main()
