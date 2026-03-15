from __future__ import annotations

from app.broker.base import AbstractBroker, OrderResult, PositionInfo
from app.core.config import Settings


class AlpacaBroker(AbstractBroker):
    """Alpaca Markets broker scaffold.

    All methods raise NotImplementedError — live trading is not yet implemented.
    Set ALPACA_API_KEY, ALPACA_API_SECRET, and ALPACA_BASE_URL in .env before
    enabling this broker.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
    ) -> OrderResult:
        raise NotImplementedError(
            "AlpacaBroker.place_order is not yet implemented. "
            "Use PaperBroker for paper trading."
        )

    def get_position(self, ticker: str) -> PositionInfo | None:
        raise NotImplementedError("AlpacaBroker.get_position is not yet implemented.")

    def get_portfolio_value(self) -> float:
        raise NotImplementedError("AlpacaBroker.get_portfolio_value is not yet implemented.")

    def get_cash(self) -> float:
        raise NotImplementedError("AlpacaBroker.get_cash is not yet implemented.")

    def cancel_all_orders(self, ticker: str | None = None) -> int:
        raise NotImplementedError("AlpacaBroker.cancel_all_orders is not yet implemented.")
