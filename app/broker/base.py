from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    ticker: str
    action: str          # BUY | SELL | SHORT | COVER
    quantity: float
    fill_price: float | None
    error: str | None = None


@dataclass
class PositionInfo:
    ticker: str
    quantity: float       # positive = long, negative = short
    avg_cost: float
    market_value: float
    unrealized_pnl: float


class AbstractBroker(ABC):
    """Broker interface — all implementations must satisfy this contract."""

    @abstractmethod
    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
    ) -> OrderResult:
        """Place a trade order. action ∈ {BUY, SELL, SHORT, COVER}."""
        ...

    @abstractmethod
    def get_position(self, ticker: str) -> PositionInfo | None:
        """Return current position for ticker, or None if flat."""
        ...

    @abstractmethod
    def get_portfolio_value(self) -> float:
        """Return total portfolio market value (cash + positions)."""
        ...

    @abstractmethod
    def get_cash(self) -> float:
        """Return available cash balance."""
        ...

    @abstractmethod
    def cancel_all_orders(self, ticker: str | None = None) -> int:
        """Cancel open orders. Returns number of orders cancelled."""
        ...
