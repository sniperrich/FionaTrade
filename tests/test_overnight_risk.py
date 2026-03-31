from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.broker.base import PositionInfo
from app.services.overnight_risk import OvernightRiskService
from app.services.runtime_control import CONTROL_OVERNIGHT_RISK_STATE


def test_summarize_account_includes_exposure_and_guard(settings) -> None:
    positions = [
        PositionInfo(ticker="AAPL", quantity=10, avg_cost=100.0, market_value=1050.0, unrealized_pnl=50.0),
        PositionInfo(ticker="MSFT", quantity=-5, avg_cost=200.0, market_value=-980.0, unrealized_pnl=20.0),
    ]

    summary = OvernightRiskService.summarize_account(
        {"equity": "100000", "cash": "25000", "buying_power": "200000"},
        positions,
        open_orders_count=3,
        settings=settings,
    )

    assert summary["gross_exposure"] == 2030.0
    assert summary["net_exposure"] == 70.0
    assert summary["unrealized_pnl_total"] == 70.0
    assert summary["open_positions_count"] == 2
    assert summary["open_orders_count"] == 3
    assert summary["risk_source_label"] == "已有持仓浮盈亏"
    assert summary["overnight_guard"]["enabled"] is True
    assert summary["overnight_guard"]["flatten_before_close"] is False
    assert summary["overnight_guard"]["mode"] == "REDUCE"


def test_effective_overnight_mode_prefers_flatten_toggle(settings) -> None:
    settings.live_flatten_before_close = True
    settings.live_overnight_mode = "REDUCE"

    assert OvernightRiskService.effective_overnight_mode(settings) == "FLATTEN"


def test_build_reduce_plan_scales_long_and_short_positions() -> None:
    positions = [
        PositionInfo(ticker="AAPL", quantity=10, avg_cost=100.0, market_value=1000.0, unrealized_pnl=0.0),
        PositionInfo(ticker="MSFT", quantity=-20, avg_cost=200.0, market_value=-4000.0, unrealized_pnl=0.0),
    ]

    plan = OvernightRiskService.build_reduce_plan(positions, target_gross_exposure=2500.0)
    planned = {item.ticker: item for item in plan}

    assert planned["AAPL"].action == "SELL"
    assert planned["AAPL"].quantity == 5
    assert planned["MSFT"].action == "COVER"
    assert planned["MSFT"].quantity == 10


def test_execute_overnight_guard_runs_once_per_et_day(session, settings, monkeypatch) -> None:
    service = OvernightRiskService(settings)
    et = ZoneInfo("America/New_York")
    now_et = datetime(2026, 3, 31, 15, 57, tzinfo=et)

    class FakeBroker:
        def __init__(self) -> None:
            self.cancelled = 0
            self.orders: list[tuple[str, str, int]] = []

        def get_account(self) -> dict[str, str]:
            return {"equity": "100000", "cash": "50000", "buying_power": "150000"}

        def get_all_positions(self) -> list[PositionInfo]:
            return [
                PositionInfo(ticker="AAPL", quantity=200, avg_cost=100.0, market_value=20000.0, unrealized_pnl=200.0),
                PositionInfo(ticker="MSFT", quantity=-100, avg_cost=200.0, market_value=-20000.0, unrealized_pnl=-100.0),
            ]

        def get_open_orders(self) -> list[dict]:
            return [{"id": "ord-1"}]

        def cancel_all_orders(self) -> int:
            self.cancelled += 1
            return 1

        def place_order(self, ticker: str, action: str, quantity: int, order_type: str = "market", time_in_force: str = "day"):
            self.orders.append((ticker, action, quantity))
            return type("OrderResult", (), {
                "success": True,
                "order_id": f"{ticker}-1",
                "ticker": ticker,
                "action": action,
                "quantity": quantity,
                "fill_price": None,
                "error": None,
            })()

    broker = FakeBroker()

    monkeypatch.setattr(
        "app.services.overnight_risk.market_session_info",
        lambda: {
            "label": "market_open",
            "tradeable": True,
            "min_until_close": 3,
            "et_time_str": "15:57 ET Tue Mar 31 2026",
        },
    )
    monkeypatch.setattr("app.services.overnight_risk.et_now", lambda: now_et)

    first = service.execute_overnight_guard(session, trigger="pytest", broker=broker)
    session.commit()

    assert first["executed"] is True
    assert first["reason"] == "reduced_exposure"
    assert first["cancelled_orders"] == 1
    assert first["submitted_orders"] >= 1
    assert broker.orders

    second = service.execute_overnight_guard(session, trigger="pytest", broker=broker)
    assert second["executed"] is False
    assert second["reason"] == "already_executed_today"

    state = service.control.get(session, CONTROL_OVERNIGHT_RISK_STATE)
    assert state is not None
    assert state["executed_et_date"] == "2026-03-31"
