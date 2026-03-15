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
        from app.db.models import Signal
        from sqlalchemy import select
        from datetime import datetime as dt, timedelta, timezone

        ticker = ticker.upper()
        # Paper engine executes pending Signal rows; we create a synthetic one here
        try:
            sig = Signal(
                ticker=ticker,
                action=action.upper(),
                confidence=80,
                horizon_min=self._settings.default_horizon_min,
                reason="agent_graph_direct_order",
                expires_at=dt.now(timezone.utc)
                + timedelta(minutes=self._settings.default_horizon_min),
            )
            self._session.add(sig)
            self._session.flush()

            result = self._engine.execute(self._session)

            executed = result.executed > 0
            fill_price = None
            if executed:
                from app.db.models import PaperFill
                from sqlalchemy import desc
                last_fill = self._session.execute(
                    select(PaperFill)
                    .where(PaperFill.ticker == ticker)
                    .order_by(desc(PaperFill.filled_at))
                    .limit(1)
                ).scalar_one_or_none()
                if last_fill:
                    fill_price = float(last_fill.fill_price or 0)

            return OrderResult(
                success=executed,
                order_id=str(sig.id),
                ticker=ticker,
                action=action.upper(),
                quantity=quantity,
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
        return PositionInfo(
            ticker=ticker.upper(),
            quantity=qty,
            avg_cost=avg_price,
            market_value=qty * avg_price,
            unrealized_pnl=float(row.unrealized_pnl or 0),
        )

    def get_portfolio_value(self) -> float:
        portfolio = self._engine.portfolio(self._session)
        return float(portfolio.get("total_value", self._settings.initial_nav))

    def get_cash(self) -> float:
        portfolio = self._engine.portfolio(self._session)
        return float(portfolio.get("cash", self._settings.initial_nav))

    def cancel_all_orders(self, ticker: str | None = None) -> int:
        # Paper engine doesn't have open orders; signals expire naturally
        return 0
