from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.module_attribution import ModuleAttributionService
from app.db.models import AgentRun, AgentScore, BacktestRun, Event, EventEvidence, RawItem


def _raw_item(source: str, url: str, title: str, item_hash: str, published_at: datetime) -> RawItem:
    return RawItem(
        source=source,
        source_tier=1,
        url=url,
        title=title,
        body=f"{title} body",
        published_at=published_at,
        ingested_at=published_at + timedelta(minutes=1),
        item_hash=item_hash,
        metadata_json={},
        processed=False,
    )


def _event(event_type: str, ticker: str, ts: datetime) -> Event:
    return Event(
        event_type=event_type,
        entities=[ticker],
        tickers=[ticker],
        severity=80,
        event_time=ts,
        confidence=85,
        validation_status="VALID",
        summary=f"{ticker} {event_type}",
    )


def _run_params(*, use_llm: bool, use_signal_validation: bool) -> dict:
    return {
        "start_date": "2026-01-01",
        "end_date": "2026-01-07",
        "use_llm": use_llm,
        "event_profile": "",
        "sources": ["sec", "yahoo_finance"],
        "min_confidence": 50,
        "min_severity": 0,
        "use_signal_validation": use_signal_validation,
        "use_tradeability_filter": True,
        "use_event_quality_filter": False,
        "flow_confirmation_enabled": True,
        "flow_confirmation_soft_gate": True,
        "flow_breakout_lookback_min": 15,
        "flow_wait_valid_minutes": 180,
    }


def _trade(event_id: int, ticker: str, *, pnl: float, flow_bucket: str, event_type: str, ts: datetime) -> dict:
    return {
        "event_id": event_id,
        "ticker": ticker,
        "event_type": event_type,
        "entry_ts": ts.isoformat(),
        "exit_ts": (ts + timedelta(minutes=60)).isoformat(),
        "pnl": pnl,
        "flow_bucket": flow_bucket,
        "tradeability_reason": "tradeable",
    }


def test_module_attribution_overview_and_run_detail(session):
    base = datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc)

    raw_a = _raw_item("sec", "https://example.com/sec-aapl", "AAPL filing", "hash-sec-aapl", base)
    raw_b = _raw_item("yahoo_finance", "https://example.com/yf-nvda", "NVDA headline", "hash-yf-nvda", base + timedelta(minutes=10))
    session.add_all([raw_a, raw_b])
    session.flush()

    event_a = _event("major_litigation", "AAPL", base)
    event_b = _event("buyback", "NVDA", base + timedelta(minutes=10))
    session.add_all([event_a, event_b])
    session.flush()

    session.add_all(
        [
            EventEvidence(
                event_id=event_a.id,
                raw_item_id=raw_a.id,
                url=raw_a.url,
                source=raw_a.source,
                source_tier=0,
                summary="SEC filing",
            ),
            EventEvidence(
                event_id=event_b.id,
                raw_item_id=raw_b.id,
                url=raw_b.url,
                source=raw_b.source,
                source_tier=2,
                summary="Yahoo headline",
            ),
        ]
    )

    run_enabled = BacktestRun(
        params=_run_params(use_llm=True, use_signal_validation=True),
        metrics={
            "total_return": 0.012,
            "win_rate": 0.50,
            "max_drawdown": -0.02,
            "validation_blocked": 3,
            "tradeability_filtered": 2,
            "quality_filtered": 0,
            "flow_bucket_counts": {"HIGH": 1, "LOW": 1},
        },
        trade_log=[
            _trade(event_a.id, "AAPL", pnl=120.0, flow_bucket="HIGH", event_type="major_litigation", ts=base + timedelta(minutes=1)),
            _trade(event_b.id, "NVDA", pnl=-40.0, flow_bucket="LOW", event_type="buyback", ts=base + timedelta(minutes=11)),
        ],
        status="DONE",
        created_at=base + timedelta(hours=1),
    )
    run_disabled = BacktestRun(
        params=_run_params(use_llm=True, use_signal_validation=False),
        metrics={
            "total_return": 0.008,
            "win_rate": 0.40,
            "max_drawdown": -0.03,
            "validation_blocked": 0,
            "tradeability_filtered": 1,
            "quality_filtered": 0,
            "flow_bucket_counts": {"LOW": 1},
        },
        trade_log=[
            _trade(event_a.id, "AAPL", pnl=80.0, flow_bucket="LOW", event_type="major_litigation", ts=base + timedelta(minutes=2)),
        ],
        status="DONE",
        created_at=base + timedelta(hours=2),
    )
    run_rules = BacktestRun(
        params=_run_params(use_llm=False, use_signal_validation=True),
        metrics={"total_return": -0.001, "win_rate": 0.2, "max_drawdown": -0.04},
        trade_log=[_trade(event_b.id, "NVDA", pnl=-20.0, flow_bucket="LOW", event_type="buyback", ts=base + timedelta(minutes=3))],
        status="DONE",
        created_at=base + timedelta(hours=3),
    )
    session.add_all([run_enabled, run_disabled, run_rules])

    session.add_all(
        [
            AgentRun(
                ticker="AAPL",
                status="COMPLETED",
                final_action="BUY",
                portfolio_output={"metadata": {"conviction": "HIGH"}},
                risk_output={"metadata": {"approved": True}},
                created_at=base + timedelta(hours=2),
            ),
            AgentRun(
                ticker="NVDA",
                status="COMPLETED",
                final_action="HOLD",
                portfolio_output={"metadata": {"conviction": "LOW"}},
                risk_output={"metadata": {"approved": False}},
                created_at=base + timedelta(hours=3),
            ),
        ]
    )
    session.add_all(
        [
            AgentScore(
                agent_name="news_sentiment",
                ticker="AAPL",
                signal="BUY",
                confidence=80,
                predicted_at=base + timedelta(hours=2),
                price_at_prediction=100.0,
                price_after=102.0,
                actual_return_pct=2.0,
                eval_horizon_days=3,
                score=35.0,
                score_reasoning="good",
            ),
            AgentScore(
                agent_name="technicals",
                ticker="AAPL",
                signal="SHORT",
                confidence=70,
                predicted_at=base + timedelta(hours=2),
                price_at_prediction=100.0,
                price_after=102.0,
                actual_return_pct=2.0,
                eval_horizon_days=3,
                score=-30.0,
                score_reasoning="bad",
            ),
        ]
    )
    session.flush()

    service = ModuleAttributionService()
    payload = service.overview(
        session,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 1, 10, tzinfo=timezone.utc),
        lookback_days=30,
        mode="llm",
        run_ids=[],
        tickers=[],
        min_sample=1,
    )

    assert payload["kpi"]["sample_runs"] == 2
    assert payload["kpi"]["sample_trades"] == 3

    event_buckets = {row["bucket"]: row for row in payload["event_type_buckets"]}
    assert "major_litigation" in event_buckets
    assert event_buckets["major_litigation"]["trades"] == 2

    tier_buckets = {row["bucket"]: row for row in payload["source_tier_buckets"]}
    assert "tier0" in tier_buckets
    assert "tier2" in tier_buckets

    filter_rows = {row["filter"]: row for row in payload["filter_value_rank"]}
    assert filter_rows["use_signal_validation"]["sample_pairs"] == 1

    assert payload["agent_contribution"]["agents"] >= 2
    conviction_rows = {row["bucket"]: row for row in payload["conviction_distribution"]}
    assert conviction_rows["HIGH"]["count"] == 1

    detail = service.run_detail(session, run_id=run_enabled.id)
    assert detail is not None
    assert detail["filter_hits"]["validation_blocked"] == 3
    detail_tiers = {row["bucket"] for row in detail["source_tier_buckets"]}
    assert "tier0" in detail_tiers
