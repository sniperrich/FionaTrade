from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.live_trading import LiveTradingService
from app.db.models import EntryPlan


def test_process_ticker_creates_and_replaces_wait_plan(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)

    class DummyGraph:
        def run(self, _session, _ticker, context=None, progress_callback=None):
            return {
                "final_action": "HOLD",
                "final_position_pct": 0.0,
                "final_reasoning": "timing not ideal",
                "execution_plan": {
                    "execution_mode": "WAIT_PULLBACK",
                    "planned_action": "BUY",
                    "planned_position_pct": 0.06,
                    "valid_for_minutes": 120,
                    "entry_plan": {"pullback_pct": 0.8},
                },
            }

    monkeypatch.setattr(service, "_get_agent_graph", lambda: DummyGraph())
    monkeypatch.setattr(service, "_latest_cached_close", lambda _session, _ticker: 100.0)

    class DummyBroker:
        pass

    result1 = service._process_ticker(
        session,
        DummyBroker(),
        "AAPL",
        portfolio_value=100_000.0,
        cycle_id="cycle001",
        msi={"et_time_str": "10:00 ET", "label": "open"},
        dry_run=False,
        run=None,
    )
    assert result1["plan_created"] is True

    result2 = service._process_ticker(
        session,
        DummyBroker(),
        "AAPL",
        portfolio_value=100_000.0,
        cycle_id="cycle002",
        msi={"et_time_str": "10:05 ET", "label": "open"},
        dry_run=False,
        run=None,
    )
    assert result2["plan_created"] is True

    plans = session.query(EntryPlan).filter(EntryPlan.ticker == "AAPL").order_by(EntryPlan.id.asc()).all()
    assert len(plans) == 2
    assert plans[0].status == "REPLACED"
    assert plans[1].status == "ACTIVE"
    assert abs(float(plans[1].target_pct) - 0.06) < 1e-9


def test_execute_active_wait_until_open_plan_triggers_order(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)

    plan = EntryPlan(
        ticker="MSFT",
        status="ACTIVE",
        execution_mode="WAIT_UNTIL_OPEN",
        planned_action="BUY",
        target_pct=0.05,
        trigger_json={},
        valid_until=datetime.now(timezone.utc).replace(microsecond=0),
    )
    session.add(plan)
    session.flush()

    monkeypatch.setattr(service.market_data, "is_ticker_cache_fresh", lambda _session, _ticker, max_age_minutes=None: (True, 1.0))

    class DummyBroker:
        def get_latest_price(self, _ticker):
            return 100.0

        def get_position(self, _ticker):
            return None

        def get_open_orders(self, _ticker):
            return []

        def place_bracket_order(self, **kwargs):
            return SimpleNamespace(success=True, order_id="ord-123", error=None)

    result = service._execute_active_entry_plans(
        session=session,
        broker=DummyBroker(),
        portfolio_value=100_000.0,
        tickers=["MSFT"],
        cycle_id="cycle-open",
        msi={"label": "open", "tradeable": True, "et_time_str": "09:31 ET"},
        run=None,
    )

    assert result["triggered_tickers"] == ["MSFT"]
    session.refresh(plan)
    assert plan.status == "TRIGGERED"
    assert plan.triggered_at is not None
