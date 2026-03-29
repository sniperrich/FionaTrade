from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes import (
    attribution_backfill_agent_scores,
    attribution_overview,
    attribution_run_detail,
)
from app.db.models import AgentRun, BacktestRun, Bar1m, Event, EventEvidence, RawItem


def test_attribution_overview_and_run_detail_api(session):
    base = datetime(2026, 1, 4, 14, 30, tzinfo=timezone.utc)
    raw = RawItem(
        source="sec",
        source_tier=0,
        url="https://example.com/sec-aapl-8k",
        title="AAPL 8-K",
        body="filing body",
        published_at=base,
        ingested_at=base + timedelta(minutes=1),
        item_hash="hash-api-sec-aapl",
        metadata_json={},
        processed=False,
    )
    session.add(raw)
    session.flush()

    event = Event(
        event_type="major_litigation",
        entities=["Apple"],
        tickers=["AAPL"],
        severity=80,
        event_time=base,
        confidence=85,
        validation_status="VALID",
        summary="AAPL major litigation",
    )
    session.add(event)
    session.flush()

    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source=raw.source,
            source_tier=0,
            summary="SEC evidence",
        )
    )
    run = BacktestRun(
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-01-08",
            "use_llm": True,
            "use_signal_validation": True,
            "use_tradeability_filter": True,
            "use_event_quality_filter": False,
            "flow_confirmation_enabled": True,
            "flow_confirmation_soft_gate": True,
            "flow_breakout_lookback_min": 15,
            "flow_wait_valid_minutes": 180,
        },
        metrics={"total_return": 0.01, "win_rate": 1.0, "max_drawdown": 0.0},
        trade_log=[
            {
                "event_id": event.id,
                "ticker": "AAPL",
                "event_type": "major_litigation",
                "entry_ts": (base + timedelta(minutes=1)).isoformat(),
                "exit_ts": (base + timedelta(minutes=31)).isoformat(),
                "pnl": 50.0,
                "flow_bucket": "HIGH",
                "tradeability_reason": "tradeable",
            }
        ],
        status="DONE",
        created_at=base + timedelta(hours=1),
    )
    session.add(run)
    session.flush()

    overview = attribution_overview(
        session=session,
        start_date="2026-01-01",
        end_date="2026-01-10",
        lookback_days=30,
        mode="all",
        run_ids=None,
        tickers=None,
        min_sample=1,
    )
    assert "agent_contribution" in overview
    assert "event_type_buckets" in overview
    assert "source_tier_buckets" in overview
    assert "filter_value_rank" in overview

    detail = attribution_run_detail(run_id=run.id, session=session)
    assert detail["run"]["id"] == run.id
    assert detail["event_type_buckets"][0]["bucket"] == "major_litigation"


def test_attribution_backfill_agent_scores_api(session):
    base = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            Bar1m(
                ticker="AAPL",
                ts=base,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
                source="test",
            ),
            Bar1m(
                ticker="AAPL",
                ts=base + timedelta(days=2),
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.0,
                volume=1000.0,
                source="test",
            ),
        ]
    )
    session.add(
        AgentRun(
            ticker="AAPL",
            status="COMPLETED",
            created_at=base,
            macro_output={"signal": "BUY", "confidence": 70, "reasoning": "test"},
            final_action="BUY",
        )
    )
    session.flush()

    result = attribution_backfill_agent_scores(
        body={"lookback_days": 365, "eval_horizon_days": 2, "limit": 100},
        session=session,
    )
    assert result["ok"] is True
    assert result["created_scores"] >= 1
