from __future__ import annotations

from sqlalchemy.orm import Session

from app.broker.base import AbstractBroker, OrderResult, PositionInfo
from app.core.config import Settings
from app.core.logging import get_app_logger
from app.paper_engine.service import PaperEngineService

logger = get_app_logger()


class PaperBroker(AbstractBroker):
    """Wraps PaperEngineService as an AbstractBroker.

    Requires a SQLAlchemy session to be injected at each call because
    PaperEngineService is session-scoped.
    """

    def __init__(self, settings: Settings, session: Session) -> None:
        self._engine = PaperEngineService(settings)
        self._settings = settings
        self._session = session

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
    ) -> OrderResult:
        ticker = ticker.upper()
        try:
            if order_type.lower() != "market":
                raise ValueError("PaperBroker only supports market orders")
            order, fill_price = self._engine.execute_direct_order(
                self._session,
                ticker=ticker,
                action=action.upper(),
                quantity=quantity,
            )

            return OrderResult(
                success=True,
                order_id=str(order.id),
                ticker=ticker,
                action=action.upper(),
                quantity=float(order.qty),
                fill_price=fill_price,
            )
        except Exception as exc:
            logger.exception("[PaperBroker] place_order failed for %s: %s", ticker, exc)
            return OrderResult(
                success=False,
                order_id=None,
                ticker=ticker,
                action=action.upper(),
                quantity=quantity,
                fill_price=None,
                error=str(exc),
            )

    def get_position(self, ticker: str) -> PositionInfo | None:
        from app.db.models import Position
        from sqlalchemy import select

        row = self._session.execute(
            select(Position).where(Position.ticker == ticker.upper())
        ).scalar_one_or_none()
        if not row:
            return None
        qty = float(row.qty)
        avg_price = float(row.avg_price or 0)
        last_price = float(row.last_price or avg_price or 0)
        return PositionInfo(
            ticker=ticker.upper(),
            quantity=qty,
            avg_cost=avg_price,
            market_value=qty * last_price,
            unrealized_pnl=float(row.unrealized_pnl or 0),
        )

    def get_portfolio_value(self) -> float:
        portfolio = self._engine.portfolio(self._session)
        return float(portfolio.get("nav", portfolio.get("total_value", self._settings.initial_nav)))

    def get_cash(self) -> float:
        portfolio = self._engine.portfolio(self._session)
        return float(portfolio.get("cash", portfolio.get("nav", self._settings.initial_nav)))

    def cancel_all_orders(self, ticker: str | None = None) -> int:
        # Paper engine doesn't have open orders; signals expire naturally
        return 0
