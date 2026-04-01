from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.market_hours import is_holiday
from app.core.utils import ensure_utc
from app.db.models import AgentRun, AgentScore, BacktestRun, Bar1m, Event, EventEvidence, LiveTrade


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


_NY = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


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
        source_tier_map, source_map = self._event_source_maps(
            session,
            event_ids={int(row.get("event_id")) for row in trade_rows if row.get("event_id") is not None},
        )

        event_type_buckets = self._bucketize(trade_rows, key_getter=lambda row: self._label(row.get("event_type"), "unknown"))
        source_tier_buckets = self._bucketize(
            trade_rows,
            key_getter=lambda row: self._source_tier_label(source_tier_map.get(self._safe_int(row.get("event_id")))),
        )
        source_buckets = self._bucketize(
            trade_rows,
            key_getter=lambda row: self._source_label(
                source_map.get(self._safe_int(row.get("event_id"))),
                fallback=row.get("source"),
            ),
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
        news_impact_report = self._news_impact_report(session, filters)

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
                news_link_coverage_pct=float((news_impact_report.get("summary") or {}).get("event_link_coverage_pct") or 0.0),
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
            "source_buckets": source_buckets,
            "flow_bucket_buckets": flow_bucket_buckets,
            "tradeability_reason_buckets": tradeability_reason_buckets,
            "conviction_distribution": agent_distributions["conviction_distribution"],
            "risk_approved_distribution": agent_distributions["risk_approved_distribution"],
            "final_action_distribution": agent_distributions["final_action_distribution"],
            "filter_value_rank": filter_value_rank,
            "news_impact_report": news_impact_report,
            "data_quality": data_quality,
        }

    def run_detail(self, session: Session, run_id: int) -> dict[str, Any] | None:
        run = session.get(BacktestRun, run_id)
        if run is None:
            return None

        payload = self._run_payload(run)
        trades = list(payload.get("trade_log") or [])
        source_tier_map, source_map = self._event_source_maps(
            session,
            event_ids={int(row.get("event_id")) for row in trades if row.get("event_id") is not None},
        )

        event_type_buckets = self._bucketize(trades, key_getter=lambda row: self._label(row.get("event_type"), "unknown"))
        source_tier_buckets = self._bucketize(
            trades,
            key_getter=lambda row: self._source_tier_label(source_tier_map.get(self._safe_int(row.get("event_id")))),
        )
        source_buckets = self._bucketize(
            trades,
            key_getter=lambda row: self._source_label(
                source_map.get(self._safe_int(row.get("event_id"))),
                fallback=row.get("source"),
            ),
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
            "source_buckets": source_buckets,
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

    def _event_source_maps(self, session: Session, event_ids: set[int]) -> tuple[dict[int, int], dict[int, str]]:
        if not event_ids:
            return {}, {}
        rows = session.execute(
            select(
                EventEvidence.event_id,
                EventEvidence.source_tier,
                EventEvidence.source,
                EventEvidence.captured_at,
                EventEvidence.id,
            ).where(
                EventEvidence.event_id.in_(sorted(event_ids))
            ).order_by(
                EventEvidence.event_id.asc(),
                func.coalesce(EventEvidence.source_tier, 9).asc(),
                EventEvidence.captured_at.asc(),
                EventEvidence.id.asc(),
            )
        ).all()
        source_tier_map: dict[int, int] = {}
        source_map: dict[int, str] = {}
        for event_id, source_tier, source, _, _ in rows:
            event_id_i = int(event_id)
            tier_i = int(source_tier) if source_tier is not None else 9
            source_i = self._source_label(source)

            if event_id_i not in source_tier_map:
                source_tier_map[event_id_i] = tier_i
            if event_id_i not in source_map:
                source_map[event_id_i] = source_i
            elif source_map[event_id_i] == "unknown_source" and source_i != "unknown_source":
                # Keep deterministic "first evidence wins", but allow replacing empty fallback.
                source_map[event_id_i] = source_i
        return source_tier_map, source_map

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

    def _news_impact_report(self, session: Session, filters: AttributionFilters) -> dict[str, Any]:
        if filters.mode == "rules":
            return {
                "summary": {
                    "submitted_entries": 0,
                    "event_linked_entries": 0,
                    "event_link_coverage_pct": 0.0,
                    "coverage_plus_60m": 0,
                    "coverage_same_close": 0,
                    "coverage_next_open": 0,
                },
                "horizon_impact": [],
                "alignment_buckets": [],
                "source_buckets": [],
                "source_tier_buckets": [],
                "event_type_buckets": [],
                "data_quality": {"warnings": ["live_news_impact_unavailable_in_rules_mode"]},
            }
        rows = self._load_live_trade_rows(session, filters)
        source_tier_map, source_map = self._event_source_maps(
            session,
            event_ids={int(row["event_id"]) for row in rows if row.get("event_id") is not None},
        )
        event_type_map = self._event_type_map(
            session,
            event_ids={int(row["event_id"]) for row in rows if row.get("event_id") is not None},
        )

        observations: list[dict[str, Any]] = []
        coverage = {"plus_60m": 0, "same_close": 0, "next_open": 0}

        for row in rows:
            entry_ts = ensure_utc(row["entry_ts"])
            entry_price = self._price_at_or_after(session, row["ticker"], entry_ts)
            if entry_price is None or entry_price <= 0:
                continue

            plus_60m_px = self._price_at_or_after(session, row["ticker"], entry_ts + timedelta(minutes=60))
            same_close_px = self._regular_close_price(session, row["ticker"], entry_ts)
            next_open_px = self._next_regular_open_price(session, row["ticker"], entry_ts)

            obs = {
                "ticker": row["ticker"],
                "entry_ts": entry_ts,
                "action": row["action"],
                "quantity": float(row["quantity"] or 0.0),
                "entry_price": entry_price,
                "event_id": row.get("event_id"),
                "event_type": event_type_map.get(row.get("event_id")) or "unlinked_event",
                "source": self._source_label(source_map.get(row.get("event_id")), fallback="unlinked_event"),
                "source_tier": self._source_tier_label(source_tier_map.get(row.get("event_id"))),
                "news_signal": row.get("news_signal") or "NO_SIGNAL",
                "alignment_bucket": self._alignment_bucket(row.get("news_signal"), row["action"]),
                "linked_event": row.get("event_id") is not None,
            }

            obs["plus_60m_pnl"] = self._impact_pnl(entry_price, plus_60m_px, obs["quantity"], obs["action"])
            obs["same_close_pnl"] = self._impact_pnl(entry_price, same_close_px, obs["quantity"], obs["action"])
            obs["next_open_pnl"] = self._impact_pnl(entry_price, next_open_px, obs["quantity"], obs["action"])

            obs["plus_60m_return_pct"] = self._impact_return_pct(entry_price, plus_60m_px, obs["action"])
            obs["same_close_return_pct"] = self._impact_return_pct(entry_price, same_close_px, obs["action"])
            obs["next_open_return_pct"] = self._impact_return_pct(entry_price, next_open_px, obs["action"])

            if obs["plus_60m_pnl"] is not None:
                coverage["plus_60m"] += 1
            if obs["same_close_pnl"] is not None:
                coverage["same_close"] += 1
            if obs["next_open_pnl"] is not None:
                coverage["next_open"] += 1
            observations.append(obs)

        total = len(observations)
        linked = sum(1 for row in observations if row.get("linked_event"))
        link_coverage_pct = (linked / total) if total else 0.0

        report = {
            "summary": {
                "submitted_entries": total,
                "event_linked_entries": linked,
                "event_link_coverage_pct": round(link_coverage_pct, 4),
                "coverage_plus_60m": coverage["plus_60m"],
                "coverage_same_close": coverage["same_close"],
                "coverage_next_open": coverage["next_open"],
            },
            "horizon_impact": self._horizon_impact_rows(observations),
            "alignment_buckets": self._impact_bucketize(observations, key_getter=lambda row: row.get("alignment_bucket") or "unknown"),
            "source_buckets": self._impact_bucketize(observations, key_getter=lambda row: row.get("source") or "unknown_source"),
            "source_tier_buckets": self._impact_bucketize(observations, key_getter=lambda row: row.get("source_tier") or "tier_unknown"),
            "event_type_buckets": self._impact_bucketize(observations, key_getter=lambda row: row.get("event_type") or "unknown"),
            "data_quality": {
                "warnings": self._news_impact_warnings(
                    total_entries=total,
                    link_coverage_pct=link_coverage_pct,
                    coverage=coverage,
                    min_sample=filters.min_sample,
                ),
            },
        }
        return report

    def _load_live_trade_rows(self, session: Session, filters: AttributionFilters) -> list[dict[str, Any]]:
        stmt = (
            select(LiveTrade, AgentRun)
            .join(AgentRun, AgentRun.id == LiveTrade.agent_run_id)
            .where(
                LiveTrade.agent_run_id.is_not(None),
                LiveTrade.action.in_(("BUY", "SHORT")),
                LiveTrade.status.in_(("submitted", "filled")),
            )
            .order_by(LiveTrade.created_at.asc(), LiveTrade.id.asc())
        )
        if filters.start_date:
            stmt = stmt.where(LiveTrade.created_at >= filters.start_date)
        if filters.end_date:
            stmt = stmt.where(LiveTrade.created_at <= filters.end_date)
        if filters.tickers:
            stmt = stmt.where(LiveTrade.ticker.in_(filters.tickers))

        out: list[dict[str, Any]] = []
        for live_trade, agent_run in session.execute(stmt).all():
            news_signal = str(((agent_run.news_output or {}).get("signal") or "NO_SIGNAL")).upper()
            final_action = str(agent_run.final_action or live_trade.action or "HOLD").upper()
            out.append(
                {
                    "live_trade_id": live_trade.id,
                    "agent_run_id": agent_run.id,
                    "ticker": str(live_trade.ticker or "").upper(),
                    "entry_ts": ensure_utc(live_trade.created_at),
                    "action": str(live_trade.action or "").upper(),
                    "quantity": float(live_trade.fill_qty or live_trade.quantity or 0.0),
                    "news_signal": news_signal,
                    "final_action": final_action,
                    "event_id": int(agent_run.trigger_event_id) if agent_run.trigger_event_id is not None else None,
                }
            )
        return out

    def _event_type_map(self, session: Session, event_ids: set[int]) -> dict[int, str]:
        if not event_ids:
            return {}
        rows = session.execute(
            select(Event.id, Event.event_type).where(Event.id.in_(sorted(event_ids)))
        ).all()
        return {int(event_id): self._label(event_type, "unknown") for event_id, event_type in rows}

    def _impact_bucketize(self, rows: list[dict[str, Any]], key_getter) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(key_getter(row))].append(row)

        out: list[dict[str, Any]] = []
        for key, items in grouped.items():
            out.append(
                {
                    "bucket": key,
                    "trades": len(items),
                    **self._impact_stats(items, "plus_60m"),
                    **self._impact_stats(items, "same_close"),
                    **self._impact_stats(items, "next_open"),
                }
            )
        out.sort(
            key=lambda row: (
                int(row.get("trades") or 0),
                float(row.get("same_close_pnl_sum") or 0.0),
            ),
            reverse=True,
        )
        return out

    def _horizon_impact_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"horizon": "plus_60m", **self._impact_stats(rows, "plus_60m")},
            {"horizon": "same_close", **self._impact_stats(rows, "same_close")},
            {"horizon": "next_open", **self._impact_stats(rows, "next_open")},
        ]

    def _impact_stats(self, rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
        pnl_key = f"{prefix}_pnl"
        ret_key = f"{prefix}_return_pct"
        observed = [row for row in rows if row.get(pnl_key) is not None and row.get(ret_key) is not None]
        sample = len(observed)
        wins = sum(1 for row in observed if float(row.get(pnl_key) or 0.0) > 0)
        pnl_sum = sum(float(row.get(pnl_key) or 0.0) for row in observed)
        ret_sum = sum(float(row.get(ret_key) or 0.0) for row in observed)
        return {
            f"{prefix}_sample": sample,
            f"{prefix}_win_rate": (wins / sample) if sample else 0.0,
            f"{prefix}_pnl_sum": pnl_sum,
            f"{prefix}_avg_pnl": (pnl_sum / sample) if sample else 0.0,
            f"{prefix}_avg_return_pct": (ret_sum / sample) if sample else 0.0,
        }

    def _price_at_or_after(self, session: Session, ticker: str, ts: datetime) -> float | None:
        row = session.execute(
            select(Bar1m.close)
            .where(Bar1m.ticker == ticker, Bar1m.ts >= ensure_utc(ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).first()
        return float(row[0]) if row and row[0] is not None else None

    def _regular_close_price(self, session: Session, ticker: str, ts: datetime) -> float | None:
        trade_day = self._effective_trade_day(ensure_utc(ts))
        open_utc, close_utc = self._session_bounds_utc(trade_day)
        row = session.execute(
            select(Bar1m.close)
            .where(
                Bar1m.ticker == ticker,
                Bar1m.ts >= open_utc,
                Bar1m.ts <= close_utc,
            )
            .order_by(Bar1m.ts.desc())
            .limit(1)
        ).first()
        return float(row[0]) if row and row[0] is not None else None

    def _next_regular_open_price(self, session: Session, ticker: str, ts: datetime) -> float | None:
        trade_day = self._effective_trade_day(ensure_utc(ts))
        next_day = self._next_trading_day(trade_day)
        open_utc, close_utc = self._session_bounds_utc(next_day)
        row = session.execute(
            select(Bar1m.open)
            .where(
                Bar1m.ticker == ticker,
                Bar1m.ts >= open_utc,
                Bar1m.ts <= close_utc,
            )
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).first()
        return float(row[0]) if row and row[0] is not None else None

    def _effective_trade_day(self, ts: datetime) -> date:
        et = ensure_utc(ts).astimezone(_NY)
        trade_day = et.date()
        if et.time() >= _MARKET_CLOSE:
            trade_day = self._next_trading_day(trade_day)
        elif et.weekday() >= 5 or is_holiday(et):
            trade_day = self._next_trading_day(trade_day)
        return trade_day

    def _next_trading_day(self, current: date) -> date:
        candidate = current + timedelta(days=1)
        while True:
            candidate_et = datetime.combine(candidate, _MARKET_OPEN, tzinfo=_NY)
            if candidate.weekday() < 5 and not is_holiday(candidate_et):
                return candidate
            candidate += timedelta(days=1)

    def _session_bounds_utc(self, trading_day: date) -> tuple[datetime, datetime]:
        open_dt = datetime.combine(trading_day, _MARKET_OPEN, tzinfo=_NY)
        close_dt = datetime.combine(trading_day, _MARKET_CLOSE, tzinfo=_NY)
        return ensure_utc(open_dt), ensure_utc(close_dt)

    @staticmethod
    def _impact_pnl(entry_price: float, target_price: float | None, qty: float, action: str) -> float | None:
        if target_price is None:
            return None
        qty_f = float(qty or 0.0)
        if qty_f <= 0 or entry_price <= 0:
            return None
        if str(action or "").upper() == "SHORT":
            return round((entry_price - target_price) * qty_f, 4)
        return round((target_price - entry_price) * qty_f, 4)

    @staticmethod
    def _impact_return_pct(entry_price: float, target_price: float | None, action: str) -> float | None:
        if target_price is None or entry_price <= 0:
            return None
        raw = ((target_price - entry_price) / entry_price) * 100.0
        if str(action or "").upper() == "SHORT":
            raw *= -1.0
        return round(raw, 4)

    @staticmethod
    def _alignment_bucket(news_signal: Any, final_action: str) -> str:
        signal = str(news_signal or "NO_SIGNAL").upper()
        action = str(final_action or "HOLD").upper()
        if signal not in {"BUY", "SHORT"}:
            return "no_signal"
        return "agree" if signal == action else "conflict"

    @staticmethod
    def _news_impact_warnings(
        *,
        total_entries: int,
        link_coverage_pct: float,
        coverage: dict[str, int],
        min_sample: int,
    ) -> list[str]:
        warnings: list[str] = []
        if total_entries < min_sample:
            warnings.append("live_news_impact_low_sample")
        if total_entries and link_coverage_pct < 0.5:
            warnings.append("live_news_event_links_low")
        if total_entries and coverage.get("same_close", 0) < total_entries:
            warnings.append("same_close_bar_coverage_incomplete")
        if total_entries and coverage.get("next_open", 0) < max(1, total_entries // 2):
            warnings.append("next_open_bar_coverage_low")
        return warnings

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
        news_link_coverage_pct: float,
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
        if 0 < news_link_coverage_pct < 0.5:
            warnings.append("live_news_event_links_low")
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
    def _source_label(value: Any, fallback: Any = None) -> str:
        primary = str(value or "").strip().lower()
        if primary:
            return primary
        fallback_text = str(fallback or "").strip().lower()
        if fallback_text:
            return fallback_text
        return "unknown_source"

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
