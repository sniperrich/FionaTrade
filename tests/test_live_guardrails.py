from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.broker.base import PositionInfo
from app.db.models import AgentRun, Event, LiveTrade
from app.services.live_trading import LiveTradingService
from app.services.runtime_control import RuntimeControlService


class _DummyBroker:
    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        positions: list[PositionInfo] | None = None,
        latest_price: float = 100.0,
    ) -> None:
        self._equity = equity
        self._positions = positions or []
        self._latest_price = latest_price

    def get_latest_price(self, _ticker: str) -> float:
        return self._latest_price

    def get_position(self, ticker: str):
        for pos in self._positions:
            if pos.ticker == ticker:
                return pos
        return None

    def get_account(self) -> dict[str, str]:
        return {"equity": str(self._equity)}

    def get_all_positions(self) -> list[PositionInfo]:
        return list(self._positions)

    def get_open_orders(self, _ticker: str):
        return []

    def place_bracket_order(self, **kwargs):
        return SimpleNamespace(success=True, order_id="ord-1", error=None, **kwargs)


def _open_msi() -> dict[str, str]:
    return {"et_time_str": "09:35 ET", "label": "open"}


def test_startup_ramp_limits_new_positions(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_startup_max_new_positions": 1,
            "live_startup_ramp_minutes": 30,
        }
    )
    service = LiveTradingService(live_settings)
    RuntimeControlService().set_live_enabled(session, live_settings, True, source="pytest")
    session.add(
        LiveTrade(
            cycle_id="prev-cycle",
            ticker="MSFT",
            action="BUY",
            quantity=10,
            target_pct=0.05,
            status="submitted",
            et_time="09:31 ET",
            market_session="open",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.flush()

    monkeypatch.setattr(
        service.market_data,
        "is_ticker_cache_fresh",
        lambda _session, _ticker, max_age_minutes=None: (True, 1.0),
    )

    result = service._submit_order_for_action(
        session=session,
        broker=_DummyBroker(),
        ticker="AAPL",
        desired_action="BUY",
        target_pct=0.05,
        portfolio_value=100_000.0,
        cycle_id="cycle-startup-cap",
        msi=_open_msi(),
        agent_run_id=None,
        reasoning="startup ramp test",
        run=None,
        trigger_event={"id": 1, "event_type": "guidance_cut"},
    )

    assert result["order_placed"] is False
    assert result["reason"] == "startup_ramp_position_cap"
    assert result["startup_limit"] == 1


def test_net_short_exposure_cap_blocks_new_short(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_startup_ramp_minutes": 0,
            "live_max_net_short_exposure_pct": 0.10,
        }
    )
    service = LiveTradingService(live_settings)
    monkeypatch.setattr(
        service.market_data,
        "is_ticker_cache_fresh",
        lambda _session, _ticker, max_age_minutes=None: (True, 1.0),
    )
    broker = _DummyBroker(
        positions=[
            PositionInfo(ticker="MSFT", quantity=-50, avg_cost=180.0, market_value=-9000.0, unrealized_pnl=0.0),
        ]
    )

    result = service._submit_order_for_action(
        session=session,
        broker=broker,
        ticker="AAPL",
        desired_action="SHORT",
        target_pct=0.05,
        portfolio_value=100_000.0,
        cycle_id="cycle-short-cap",
        msi=_open_msi(),
        agent_run_id=None,
        reasoning="net short cap test",
        run=None,
        trigger_event={"id": 2, "event_type": "macro_shock"},
    )

    assert result["order_placed"] is False
    assert result["reason"] == "net_short_exposure_cap"
    assert result["max_short_exposure_pct"] == 0.10


def test_same_theme_direction_cap_blocks_duplicate_cluster(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_startup_ramp_minutes": 0,
            "live_max_same_theme_direction_positions": 1,
            "live_max_same_direction_positions": 5,
            "live_max_net_short_exposure_pct": 0.50,
        }
    )
    service = LiveTradingService(live_settings)
    monkeypatch.setattr(
        service.market_data,
        "is_ticker_cache_fresh",
        lambda _session, _ticker, max_age_minutes=None: (True, 1.0),
    )

    event = Event(
        event_type="guidance_cut",
        entities=["MSFT"],
        tickers=["MSFT"],
        severity=80,
        event_time=datetime.now(timezone.utc),
        confidence=90,
        validation_status="VALID",
        summary="MSFT guidance cut",
    )
    session.add(event)
    session.flush()
    run = AgentRun(
        ticker="MSFT",
        trigger="event",
        trigger_event_id=event.id,
        status="COMPLETED",
    )
    session.add(run)
    session.flush()
    session.add(
        LiveTrade(
            cycle_id="prev-cycle",
            ticker="MSFT",
            agent_run_id=run.id,
            action="SHORT",
            quantity=10,
            target_pct=0.04,
            status="submitted",
            et_time="10:02 ET",
            market_session="open",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.flush()

    broker = _DummyBroker(
        positions=[
            PositionInfo(ticker="MSFT", quantity=-10, avg_cost=100.0, market_value=-1000.0, unrealized_pnl=0.0),
        ]
    )

    result = service._submit_order_for_action(
        session=session,
        broker=broker,
        ticker="AAPL",
        desired_action="SHORT",
        target_pct=0.04,
        portfolio_value=100_000.0,
        cycle_id="cycle-theme-cap",
        msi=_open_msi(),
        agent_run_id=None,
        reasoning="theme cap test",
        run=None,
        trigger_event={"id": 3, "event_type": "guidance_cut"},
    )

    assert result["order_placed"] is False
    assert result["reason"] == "same_theme_direction_cap"
    assert result["event_type"] == "guidance_cut"
