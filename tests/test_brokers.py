from __future__ import annotations

from app.broker.alpaca import AlpacaBroker
from app.broker.paper import PaperBroker
from app.db.models import Bar1m, Position
from app.core.utils import utc_now


def test_paper_broker_places_market_order_with_requested_quantity(session, settings) -> None:
    now = utc_now()
    session.add(
        Bar1m(
            ticker="AAPL",
            ts=now,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
            source="test",
        )
    )
    session.flush()

    broker = PaperBroker(settings, session)
    result = broker.place_order("AAPL", "BUY", 3.0)
    position = session.query(Position).filter(Position.ticker == "AAPL").one()

    assert result.success is True
    assert result.quantity == 3.0
    assert position.qty == 3.0
    assert broker.get_position("AAPL").market_value == 3.0 * 100.0


def test_paper_broker_rejects_non_market_orders(session, settings) -> None:
    broker = PaperBroker(settings, session)

    result = broker.place_order("AAPL", "BUY", 1.0, order_type="limit")

    assert result.success is False
    assert "market orders" in str(result.error)


def test_alpaca_broker_place_order_posts_expected_payload(settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "alpaca_api_key": "key",
            "alpaca_api_secret": "secret",
            "alpaca_base_url": "https://paper-api.alpaca.markets",
        }
    )
    broker = AlpacaBroker(live_settings)
    captured: dict[str, object] = {}

    def _fake_post(path: str, body: dict) -> dict:
        captured["path"] = path
        captured["body"] = body
        return {"id": "ord-1", "status": "accepted", "filled_avg_price": "123.45"}

    monkeypatch.setattr(broker, "_post", _fake_post)
    result = broker.place_order("aapl", "BUY", 2.0)

    assert result.success is True
    assert result.fill_price == 123.45
    assert captured["path"] == "/orders"
    assert captured["body"]["symbol"] == "AAPL"
    assert captured["body"]["qty"] == "2"
    assert captured["body"]["side"] == "buy"
    assert captured["body"]["type"] == "market"


def test_alpaca_broker_place_bracket_order_posts_exit_legs(settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "alpaca_api_key": "key",
            "alpaca_api_secret": "secret",
            "alpaca_base_url": "https://paper-api.alpaca.markets",
        }
    )
    broker = AlpacaBroker(live_settings)
    captured: dict[str, object] = {}

    def _fake_post(path: str, body: dict) -> dict:
        captured["path"] = path
        captured["body"] = body
        return {"id": "bracket-1", "status": "accepted"}

    monkeypatch.setattr(broker, "_post", _fake_post)
    result = broker.place_bracket_order(
        ticker="NVDA",
        action="BUY",
        quantity=5.0,
        take_profit_price=120.0,
        stop_loss_price=95.0,
    )

    assert result.success is True
    assert captured["path"] == "/orders"
    assert captured["body"]["order_class"] == "bracket"
    assert captured["body"]["take_profit"]["limit_price"] == "120.00"
    assert captured["body"]["stop_loss"]["stop_price"] == "95.00"
