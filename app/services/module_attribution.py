from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Iterable

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.utils import ensure_utc
from app.db.models import AgentRun, AgentScore, BacktestRun, EventEvidence


_FILTER_KEYS = (
    "use_signal_validation",
    "use_tradeability_filter",
    "use_event_quality_filter",
    "flow_confirmation_enabled",
    "flow_confirmation_soft_gate",
)


@dataclass
class AttributionFilters:
    start_date: datetime | None
    end_date: datetime | None
    mode: str
    run_ids: list[int]
    tickers: list[str]
    min_sample: int


class ModuleAttributionService:
    """Aggregates module attribution metrics for backtests and live agent runs."""

    def overview(
        self,
        session: Session,
        *,
        start_date: datetime | None,
        end_date: datetime | None,
        lookback_days: int,
        mode: str,
        run_ids: list[int] | None,
        tickers: list[str] | None,
        min_sample: int,
    ) -> dict[str, Any]:
        window_start, window_end = self._resolve_window(start_date, end_date, lookback_days)
        filters = AttributionFilters(
            start_date=window_start,
            end_date=window_end,
            mode=self._normalize_mode(mode),
            run_ids=sorted(set(run_ids or [])),
            tickers=sorted({str(t).upper() for t in (tickers or []) if str(t).strip()}),
            min_sample=max(1, int(min_sample or 1)),
        )

        runs = self._load_backtest_runs(session, filters)
        run_payloads = [self._run_payload(run) for run in runs]
        trade_rows = self._collect_trade_rows(run_payloads)
        if filters.tickers:
            ticker_set = set(filters.tickers)
            trade_rows = [row for row in trade_rows if str(row.get("ticker") or "").upper() in ticker_set]
        source_tier_map = self._event_source_tier_map(
            session,
            event_ids={int(row.get("event_id")) for row in trade_rows if row.get("event_id") is not None},
        )

        event_type_buckets = self._bucketize(trade_rows, key_getter=lambda row: self._label(row.get("event_type"), "unknown"))
        source_tier_buckets = self._bucketize(
            trade_rows,
            key_getter=lambda row: self._source_tier_label(source_tier_map.get(self._safe_int(row.get("event_id")))),
        )
        flow_bucket_buckets = self._bucketize(trade_rows, key_getter=lambda row: self._label(row.get("flow_bucket"), "UNKNOWN"))
        tradeability_reason_buckets = self._bucketize(
            trade_rows,
            key_getter=lambda row: self._label(row.get("tradeability_reason"), "unknown"),
        )

        filter_value_rank = self._compute_filter_value_rank(run_payloads, min_sample=filters.min_sample)

        agent_runs = self._load_agent_runs(session, filters)
        agent_distributions = self._agent_distribution(agent_runs)
        agent_contribution = self._agent_contribution(session, filters, min_sample=filters.min_sample)

        coverage_scored = agent_contribution.get("scored_predictions", 0)
        coverage_runs = max(1, int(agent_contribution.get("total_completed_agent_runs", 0)))
        score_coverage_pct = round((coverage_scored / coverage_runs) * 100.0, 2) if coverage_runs > 0 else 0.0

        total_return = sum(float((row.get("metrics") or {}).get("total_return") or 0.0) for row in run_payloads)
        max_drawdown = self._global_max_drawdown([float((row.get("metrics") or {}).get("max_drawdown") or 0.0) for row in run_payloads])

        comparable_pairs = sum(int(item.get("sample_pairs", 0)) for item in filter_value_rank)
        data_quality = {
            "backtest_runs": len(run_payloads),
            "backtest_trades": len(trade_rows),
            "agent_runs": len(agent_runs),
            "agent_scored_predictions": coverage_scored,
            "agent_score_coverage_pct": score_coverage_pct,
            "comparable_filter_pairs": comparable_pairs,
            "warnings": self._build_quality_warnings(
                run_count=len(run_payloads),
                trade_count=len(trade_rows),
                scored_predictions=coverage_scored,
                comparable_pairs=comparable_pairs,
                min_sample=filters.min_sample,
            ),
        }

        return {
            "filters": {
                "start_date": window_start.isoformat() if window_start else None,
                "end_date": window_end.isoformat() if window_end else None,
                "lookback_days": lookback_days,
                "mode": filters.mode,
                "run_ids": filters.run_ids,
                "tickers": filters.tickers,
                "min_sample": filters.min_sample,
            },
            "kpi": {
                "sample_runs": len(run_payloads),
                "sample_trades": len(trade_rows),
                "coverage_pct": score_coverage_pct,
                "total_return": total_return,
                "overall_drawdown": max_drawdown,
                "comparable_pairs": comparable_pairs,
            },
            "agent_contribution": agent_contribution,
            "event_type_buckets": event_type_buckets,
            "source_tier_buckets": source_tier_buckets,
            "flow_bucket_buckets": flow_bucket_buckets,
            "tradeability_reason_buckets": tradeability_reason_buckets,
            "conviction_distribution": agent_distributions["conviction_distribution"],
            "risk_approved_distribution": agent_distributions["risk_approved_distribution"],
            "final_action_distribution": agent_distributions["final_action_distribution"],
            "filter_value_rank": filter_value_rank,
            "data_quality": data_quality,
        }

    def run_detail(self, session: Session, run_id: int) -> dict[str, Any] | None:
        run = session.get(BacktestRun, run_id)
        if run is None:
            return None

        payload = self._run_payload(run)
        trades = list(payload.get("trade_log") or [])
        source_tier_map = self._event_source_tier_map(
            session,
            event_ids={int(row.get("event_id")) for row in trades if row.get("event_id") is not None},
        )

        event_type_buckets = self._bucketize(trades, key_getter=lambda row: self._label(row.get("event_type"), "unknown"))
        source_tier_buckets = self._bucketize(
            trades,
            key_getter=lambda row: self._source_tier_label(source_tier_map.get(self._safe_int(row.get("event_id")))),
        )
        flow_bucket_buckets = self._bucketize(trades, key_getter=lambda row: self._label(row.get("flow_bucket"), "UNKNOWN"))
        tradeability_reason_buckets = self._bucketize(
            trades,
            key_getter=lambda row: self._label(row.get("tradeability_reason"), "unknown"),
        )

        metrics = payload.get("metrics") or {}
        filter_hits = {
            "validation_blocked": int(metrics.get("validation_blocked") or 0),
            "tradeability_filtered": int(metrics.get("tradeability_filtered") or 0),
            "quality_filtered": int(metrics.get("quality_filtered") or 0),
            "profile_filtered": int(metrics.get("profile_filtered") or 0),
            "dedup_dropped": int(metrics.get("dedup_dropped") or 0),
            "routine_filing_skipped": int(metrics.get("routine_filing_skipped") or 0),
            "tradeability_reason_counts": dict(metrics.get("tradeability_reason_counts") or {}),
            "flow_bucket_counts": dict(metrics.get("flow_bucket_counts") or {}),
        }

        return {
            "run": {
                "id": payload.get("id"),
                "status": payload.get("status"),
                "created_at": payload.get("created_at"),
                "finished_at": payload.get("finished_at"),
                "params": payload.get("params") or {},
                "metrics": metrics,
            },
            "event_type_buckets": event_type_buckets,
            "source_tier_buckets": source_tier_buckets,
            "flow_bucket_buckets": flow_bucket_buckets,
            "tradeability_reason_buckets": tradeability_reason_buckets,
            "filter_hits": filter_hits,
        }

    def _resolve_window(
        self,
        start_date: datetime | None,
        end_date: datetime | None,
        lookback_days: int,
    ) -> tuple[datetime | None, datetime | None]:
        if start_date and end_date:
            return ensure_utc(start_date), ensure_utc(end_date)
        now = datetime.now(timezone.utc)
        span_days = max(1, int(lookback_days or 30))
        return now - timedelta(days=span_days), now

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        normalized = str(mode or "all").strip().lower()
        if normalized in {"llm", "rules", "all"}:
            return normalized
        return "all"

    def _load_backtest_runs(self, session: Session, filters: AttributionFilters) -> list[BacktestRun]:
        stmt = select(BacktestRun).order_by(desc(BacktestRun.created_at), desc(BacktestRun.id))
        runs = list(session.execute(stmt).scalars().all())

        out: list[BacktestRun] = []
        for run in runs:
            if filters.run_ids and run.id not in filters.run_ids:
                continue

            params = run.params or {}
            if filters.mode == "llm" and not bool(params.get("use_llm", False)):
                continue
            if filters.mode == "rules" and bool(params.get("use_llm", False)):
                continue

            window_start = filters.start_date
            window_end = filters.end_date
            if window_start or window_end:
                run_start = self._parse_iso_dt(params.get("start_date"))
                run_end = self._parse_iso_dt(params.get("end_date"))
                if run_end and window_start and run_end < window_start:
                    continue
                if run_start and window_end and run_start > window_end:
                    continue

            out.append(run)
        return out

    def _load_agent_runs(self, session: Session, filters: AttributionFilters) -> list[AgentRun]:
        stmt = select(AgentRun).order_by(desc(AgentRun.created_at), desc(AgentRun.id))
        if filters.start_date:
            stmt = stmt.where(AgentRun.created_at >= filters.start_date)
        if filters.end_date:
            stmt = stmt.where(AgentRun.created_at <= filters.end_date)
        if filters.tickers:
            stmt = stmt.where(AgentRun.ticker.in_(filters.tickers))
        return list(session.execute(stmt).scalars().all())

    def _agent_distribution(self, agent_runs: Iterable[AgentRun]) -> dict[str, list[dict[str, Any]]]:
        conviction = Counter()
        risk_approved = Counter()
        final_action = Counter()

        for row in agent_runs:
            portfolio_metadata = ((row.portfolio_output or {}).get("metadata") or {})
            conviction[str(portfolio_metadata.get("conviction") or "UNKNOWN").upper()] += 1

            risk_meta = ((row.risk_output or {}).get("metadata") or {})
            approved = bool(risk_meta.get("approved", False))
            risk_approved["APPROVED" if approved else "REJECTED"] += 1

            final_action[str(row.final_action or "UNKNOWN").upper()] += 1

        return {
            "conviction_distribution": self._counter_rows(conviction),
            "risk_approved_distribution": self._counter_rows(risk_approved),
            "final_action_distribution": self._counter_rows(final_action),
        }

    def _agent_contribution(self, session: Session, filters: AttributionFilters, min_sample: int) -> dict[str, Any]:
        stmt = select(AgentScore)
        if filters.start_date:
            stmt = stmt.where(AgentScore.predicted_at >= filters.start_date)
        if filters.end_date:
            stmt = stmt.where(AgentScore.predicted_at <= filters.end_date)
        if filters.tickers:
            stmt = stmt.where(AgentScore.ticker.in_(filters.tickers))

        rows = list(session.execute(stmt).scalars().all())
        grouped: dict[str, list[AgentScore]] = defaultdict(list)
        for row in rows:
            grouped[str(row.agent_name or "unknown")].append(row)

        baseline_return = 0.0
        if rows:
            baseline_return = sum(float(row.actual_return_pct or 0.0) for row in rows) / len(rows)

        ranking: list[dict[str, Any]] = []
        for agent_name, scores in grouped.items():
            predictions = len(scores)
            hit_rate = (sum(1 for s in scores if float(s.score or 0.0) > 0) / predictions) if predictions else 0.0
            avg_score = (sum(float(s.score or 0.0) for s in scores) / predictions) if predictions else 0.0
            avg_actual_return = (
                sum(float(s.actual_return_pct or 0.0) for s in scores) / predictions if predictions else 0.0
            )
            ranking.append(
                {
                    "agent": agent_name,
                    "predictions": predictions,
                    "hit_rate": hit_rate,
                    "avg_score": avg_score,
                    "avg_actual_return": avg_actual_return,
                    "uplift_vs_baseline": avg_actual_return - baseline_return,
                    "low_sample": predictions < max(1, min_sample),
                }
            )

        ranking.sort(
            key=lambda row: (
                float(row.get("uplift_vs_baseline") or 0.0),
                float(row.get("avg_score") or 0.0),
                int(row.get("predictions") or 0),
            ),
            reverse=True,
        )

        total_completed_agent_runs = self._count_completed_agent_runs(session, filters)
        return {
            "baseline_actual_return": baseline_return,
            "rows": ranking,
            "scored_predictions": len(rows),
            "agents": len(ranking),
            "total_completed_agent_runs": total_completed_agent_runs,
        }

    def _count_completed_agent_runs(self, session: Session, filters: AttributionFilters) -> int:
        stmt = select(func.count(AgentRun.id)).where(AgentRun.status == "COMPLETED")
        if filters.start_date:
            stmt = stmt.where(AgentRun.created_at >= filters.start_date)
        if filters.end_date:
            stmt = stmt.where(AgentRun.created_at <= filters.end_date)
        if filters.tickers:
            stmt = stmt.where(AgentRun.ticker.in_(filters.tickers))
        return int(session.execute(stmt).scalar_one() or 0)

    def _run_payload(self, run: BacktestRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "params": dict(run.params or {}),
            "metrics": dict(run.metrics or {}),
            "trade_log": list(run.trade_log or []),
        }

    def _collect_trade_rows(self, run_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for run in run_payloads:
            metrics = run.get("metrics") or {}
            for trade in list(run.get("trade_log") or []):
                row = dict(trade or {})
                row["run_id"] = run.get("id")
                row["run_created_at"] = run.get("created_at")
                row["run_status"] = run.get("status")
                row["use_llm"] = bool((run.get("params") or {}).get("use_llm", False))
                row["run_total_return"] = float(metrics.get("total_return") or 0.0)
                row["run_win_rate"] = float(metrics.get("win_rate") or 0.0)
                row["run_max_drawdown"] = float(metrics.get("max_drawdown") or 0.0)
                out.append(row)
        out.sort(key=lambda row: (self._parse_iso_dt(row.get("entry_ts")) or datetime.min.replace(tzinfo=timezone.utc)))
        return out

    def _event_source_tier_map(self, session: Session, event_ids: set[int]) -> dict[int, int]:
        if not event_ids:
            return {}
        rows = session.execute(
            select(EventEvidence.event_id, EventEvidence.source_tier).where(EventEvidence.event_id.in_(sorted(event_ids)))
        ).all()
        out: dict[int, int] = {}
        for event_id, source_tier in rows:
            event_id_i = int(event_id)
            tier_i = int(source_tier) if source_tier is not None else 9
            if event_id_i not in out:
                out[event_id_i] = tier_i
            else:
                out[event_id_i] = min(out[event_id_i], tier_i)
        return out

    def _bucketize(self, rows: list[dict[str, Any]], key_getter) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(key_getter(row))].append(row)

        out: list[dict[str, Any]] = []
        for key, items in grouped.items():
            pnls = [float(item.get("pnl") or 0.0) for item in items]
            wins = sum(1 for pnl in pnls if pnl > 0)
            total = len(items)
            out.append(
                {
                    "bucket": key,
                    "trades": total,
                    "wins": wins,
                    "win_rate": (wins / total) if total else 0.0,
                    "pnl_sum": sum(pnls),
                    "avg_pnl": (sum(pnls) / total) if total else 0.0,
                    "bucket_drawdown": self._bucket_drawdown(pnls),
                }
            )

        out.sort(key=lambda row: (int(row.get("trades") or 0), float(row.get("pnl_sum") or 0.0)), reverse=True)
        return out

    def _compute_filter_value_rank(self, run_payloads: list[dict[str, Any]], min_sample: int) -> list[dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in run_payloads:
            signature = self._filter_pair_signature(row)
            grouped[signature].append(row)

        deltas: dict[str, list[tuple[float, float, float]]] = defaultdict(list)

        for candidates in grouped.values():
            for left, right in combinations(candidates, 2):
                l_params = left.get("params") or {}
                r_params = right.get("params") or {}
                diff_keys = [
                    key for key in _FILTER_KEYS
                    if bool(l_params.get(key, False)) != bool(r_params.get(key, False))
                ]
                if len(diff_keys) != 1:
                    continue
                key = diff_keys[0]
                left_enabled = bool(l_params.get(key, False))
                right_enabled = bool(r_params.get(key, False))
                if left_enabled == right_enabled:
                    continue

                enabled, disabled = (left, right) if left_enabled else (right, left)
                em = enabled.get("metrics") or {}
                dm = disabled.get("metrics") or {}
                deltas[key].append(
                    (
                        float(em.get("total_return") or 0.0) - float(dm.get("total_return") or 0.0),
                        float(em.get("win_rate") or 0.0) - float(dm.get("win_rate") or 0.0),
                        float(em.get("max_drawdown") or 0.0) - float(dm.get("max_drawdown") or 0.0),
                    )
                )

        rank_rows: list[dict[str, Any]] = []
        for key in _FILTER_KEYS:
            pairs = deltas.get(key, [])
            sample_pairs = len(pairs)
            if sample_pairs:
                ret = sum(item[0] for item in pairs) / sample_pairs
                win = sum(item[1] for item in pairs) / sample_pairs
                dd = sum(item[2] for item in pairs) / sample_pairs
            else:
                ret = 0.0
                win = 0.0
                dd = 0.0

            rank_rows.append(
                {
                    "filter": key,
                    "delta_return": ret,
                    "delta_win_rate": win,
                    "delta_drawdown": dd,
                    "sample_pairs": sample_pairs,
                    "confidence": "HIGH" if sample_pairs >= max(3, min_sample) else "LOW",
                }
            )

        rank_rows.sort(
            key=lambda row: (float(row.get("delta_return") or 0.0), int(row.get("sample_pairs") or 0)),
            reverse=True,
        )
        return rank_rows

    def _filter_pair_signature(self, run_payload: dict[str, Any]) -> tuple[Any, ...]:
        params = dict(run_payload.get("params") or {})
        sources = tuple(sorted(str(item).strip().lower() for item in (params.get("sources") or [])))
        return (
            params.get("start_date"),
            params.get("end_date"),
            bool(params.get("use_llm", False)),
            str(params.get("event_profile") or ""),
            int(params.get("min_confidence") or 0),
            int(params.get("min_severity") or 0),
            int(params.get("flow_breakout_lookback_min") or 0),
            int(params.get("flow_wait_valid_minutes") or 0),
            sources,
        )

    @staticmethod
    def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
        total = sum(counter.values())
        rows = []
        for key, count in counter.items():
            rows.append(
                {
                    "bucket": key,
                    "count": int(count),
                    "ratio": (count / total) if total else 0.0,
                }
            )
        rows.sort(key=lambda item: int(item["count"]), reverse=True)
        return rows

    @staticmethod
    def _global_max_drawdown(drawdowns: list[float]) -> float:
        if not drawdowns:
            return 0.0
        return min(drawdowns)

    @staticmethod
    def _build_quality_warnings(
        *,
        run_count: int,
        trade_count: int,
        scored_predictions: int,
        comparable_pairs: int,
        min_sample: int,
    ) -> list[str]:
        warnings: list[str] = []
        if run_count < min_sample:
            warnings.append("backtest_runs_low_sample")
        if trade_count < min_sample:
            warnings.append("backtest_trades_low_sample")
        if scored_predictions < min_sample:
            warnings.append("agent_scores_low_sample")
        if comparable_pairs < min_sample:
            warnings.append("filter_pairs_low_sample")
        return warnings

    @staticmethod
    def _parse_iso_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return ensure_utc(value)
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            return ensure_utc(dt)
        except ValueError:
            return None

    @staticmethod
    def _label(value: Any, default: str) -> str:
        text = str(value or "").strip()
        if not text:
            return default
        return text

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _source_tier_label(source_tier: int | None) -> str:
        if source_tier is None:
            return "tier_unknown"
        if source_tier <= 0:
            return "tier0"
        if source_tier == 1:
            return "tier1"
        if source_tier == 2:
            return "tier2"
        return f"tier{source_tier}"

    @staticmethod
    def _bucket_drawdown(pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        for pnl in pnls:
            equity += float(pnl)
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return drawdown
