from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db.models import EntryPlan, WorkerRunEvent
from app.services.live_trading import LiveTradingService


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


@pytest.mark.parametrize("label", ["open", "market_open"])
def test_execute_active_wait_until_open_plan_triggers_order(session, settings, monkeypatch, label: str) -> None:
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

        def get_account(self):
            return {"equity": "100000"}

        def get_all_positions(self):
            return []

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
        msi={"label": label, "tradeable": True, "et_time_str": "09:31 ET"},
        run=None,
    )

    assert result["triggered_tickers"] == ["MSFT"]
    session.refresh(plan)
    assert plan.status == "TRIGGERED"
    assert plan.triggered_at is not None


@pytest.mark.parametrize("label", ["open", "market_open"])
def test_execute_active_plan_emits_trigger_log_event(session, settings, monkeypatch, label: str) -> None:
    service = LiveTradingService(settings)

    plan = EntryPlan(
        ticker="NVDA",
        status="ACTIVE",
        execution_mode="WAIT_UNTIL_OPEN",
        planned_action="BUY",
        target_pct=0.05,
        trigger_json={},
        valid_until=datetime.now(timezone.utc).replace(microsecond=0),
    )
    session.add(plan)
    session.flush()
    run = service.runtime.start_run(
        session,
        run_type="live_cycle",
        trigger="manual",
        run_key="planlog01",
    )

    monkeypatch.setattr(
        service.market_data,
        "is_ticker_cache_fresh",
        lambda _session, _ticker, max_age_minutes=None: (True, 1.0),
    )

    class DummyBroker:
        def get_latest_price(self, _ticker):
            return 100.0

        def get_position(self, _ticker):
            return None

        def get_account(self):
            return {"equity": "100000"}

        def get_all_positions(self):
            return []

        def get_open_orders(self, _ticker):
            return []

        def place_bracket_order(self, **kwargs):
            return SimpleNamespace(success=True, order_id="ord-log-1", error=None)

    service._execute_active_entry_plans(
        session=session,
        broker=DummyBroker(),
        portfolio_value=100_000.0,
        tickers=["NVDA"],
        cycle_id="cycle-log",
        msi={"label": label, "tradeable": True, "et_time_str": "09:31 ET"},
        run=run,
    )

    event = (
        session.query(WorkerRunEvent)
        .filter(WorkerRunEvent.run_key == "planlog01", WorkerRunEvent.stage == "entry_plan_triggered")
        .order_by(WorkerRunEvent.id.desc())
        .first()
    )
    assert event is not None
    payload = event.payload_json or {}
    assert payload.get("plan_id") == plan.id
    assert payload.get("status") == "triggered"


def test_flow_soft_gate_downgrades_to_wait_breakout(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)

    class DummyGraph:
        def run(self, _session, _ticker, context=None, progress_callback=None):
            return {
                "final_action": "BUY",
                "final_position_pct": 0.10,
                "final_reasoning": "news catalyst strong",
                "portfolio_manager_result": {
                    "confidence": 80,
                    "metadata": {"action": "BUY", "position_pct": 0.10},
                },
                "execution_plan": {"execution_mode": "IMMEDIATE"},
            }

    monkeypatch.setattr(service, "_get_agent_graph", lambda: DummyGraph())
    monkeypatch.setattr(service, "_latest_cached_close", lambda _session, _ticker: 100.0)
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 99,
            "event_type": "guidance_cut",
            "confidence": 85,
            "high_quality_source_count": 2,
        },
    )
    monkeypatch.setattr(
        service.capital_confirmation,
        "evaluate",
        lambda _session, ticker, direction: {
            "flow_score": 30,
            "flow_bucket": "WEAK",
            "position_multiplier": 0.35,
        },
    )

    class DummyBroker:
        pass

    result = service._process_ticker(
        session,
        DummyBroker(),
        "AAPL",
        portfolio_value=100_000.0,
        cycle_id="flowgate1",
        msi={"et_time_str": "10:10 ET", "label": "open", "tradeable": True},
        dry_run=False,
        run=None,
    )

    assert result["action"] == "HOLD"
    assert result["plan_created"] is True
    assert result["plan_mode"] == "WAIT_BREAKOUT_CONFIRMATION"
    assert result["flow_score"] == 30
    assert abs(float(result["position_multiplier"]) - 0.35) < 1e-9

    latest_plan = (
        session.query(EntryPlan)
        .filter(EntryPlan.ticker == "AAPL", EntryPlan.status == "ACTIVE")
        .order_by(EntryPlan.id.desc())
        .first()
    )
    assert latest_plan is not None
    assert latest_plan.execution_mode == "WAIT_BREAKOUT_CONFIRMATION"
    assert abs(float(latest_plan.target_pct) - 0.035) < 1e-9


def test_flow_soft_gate_trims_existing_position_without_adding(session, settings, monkeypatch) -> None:
    service = LiveTradingService(settings)

    class DummyGraph:
        def run(self, _session, _ticker, context=None, progress_callback=None):
            return {
                "final_action": "BUY",
                "final_position_pct": 0.10,
                "final_reasoning": "news catalyst strong",
                "portfolio_manager_result": {
                    "confidence": 80,
                    "metadata": {"action": "BUY", "position_pct": 0.10},
                },
                "execution_plan": {"execution_mode": "IMMEDIATE"},
            }

    monkeypatch.setattr(service, "_get_agent_graph", lambda: DummyGraph())
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 77,
            "event_type": "contract_award",
            "confidence": 90,
            "high_quality_source_count": 2,
        },
    )
    monkeypatch.setattr(service.market_data, "is_ticker_cache_fresh", lambda *_args, **_kwargs: (True, 1.0))
    monkeypatch.setattr(
        service.capital_confirmation,
        "evaluate",
        lambda _session, ticker, direction: {
            "flow_score": 30,
            "flow_bucket": "WEAK",
            "position_multiplier": 0.35,
        },
    )

    placed_market_orders: list[dict] = []
    placed_bracket_orders: list[dict] = []

    class DummyBroker:
        def get_latest_price(self, _ticker):
            return 100.0

        def get_position(self, _ticker):
            return SimpleNamespace(quantity=60)

        def get_open_orders(self, _ticker):
            return []

        def place_order(self, **kwargs):
            placed_market_orders.append(kwargs)
            return SimpleNamespace(success=True, order_id="ord-trim-1", error=None)

        def place_bracket_order(self, **kwargs):
            placed_bracket_orders.append(kwargs)
            return SimpleNamespace(success=True, order_id="ord-bracket-1", error=None)

    result = service._process_ticker(
        session,
        DummyBroker(),
        "AAPL",
        portfolio_value=100_000.0,
        cycle_id="flowtrim1",
        msi={"et_time_str": "10:15 ET", "label": "open", "tradeable": True},
        dry_run=False,
        run=None,
    )

    assert result["action"] == "SELL"
    assert result["order_placed"] is True
    assert result["flow_score"] == 30
    assert result["flow_manage_mode"] == "MANAGE_EXISTING_ONLY"
    assert result["stop_loss"] is None
    assert result["take_profit"] is None
    assert len(placed_market_orders) == 1
    assert not placed_bracket_orders
    assert placed_market_orders[0]["action"] == "SELL"
    assert placed_market_orders[0]["quantity"] == 25
