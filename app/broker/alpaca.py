"""Alpaca Markets broker — paper and live trading via REST API v2."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from app.broker.base import AbstractBroker, OrderResult, PositionInfo
from app.core.config import Settings

logger = logging.getLogger(__name__)

_API_VERSION = "v2"


class AlpacaBroker(AbstractBroker):
    """Alpaca Markets broker — supports paper and live trading.

    Paper trading endpoint : https://paper-api.alpaca.markets
    Live trading endpoint  : https://api.alpaca.markets

    Set ALPACA_API_KEY, ALPACA_API_SECRET, and ALPACA_BASE_URL in .env.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        base = (settings.alpaca_base_url or "https://paper-api.alpaca.markets").rstrip("/")
        self._base_url = f"{base}/{_API_VERSION}"
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._base_url}{path}"
        resp = requests.post(url, headers=self._headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> requests.Response:
        url = f"{self._base_url}{path}"
        resp = requests.delete(url, headers=self._headers, timeout=15)
        return resp

    # ------------------------------------------------------------------
    # AbstractBroker interface
    # ------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
    ) -> OrderResult:
        """Place an order. action ∈ {BUY, SELL, SHORT, COVER}.

        BUY   → side=buy  (open long)
        SELL  → side=sell (close long)
        SHORT → side=sell (open short — shorting_enabled must be True)
        COVER → side=buy  (close short)
        """
        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"

        # Alpaca requires integer qty for US stocks unless fractional flag set
        qty_str = str(int(quantity)) if quantity == int(quantity) else str(round(quantity, 6))

        body: dict = {
            "symbol": ticker.upper(),
            "qty": qty_str,
            "side": side,
            "type": order_type.lower(),
            "time_in_force": "day",
        }

        try:
            data = self._post("/orders", body)
            fill_price = float(data.get("filled_avg_price") or 0) or None
            logger.info(
                "[alpaca] Order placed: %s %s %s qty=%s id=%s status=%s",
                action, ticker, order_type, qty_str,
                data.get("id"), data.get("status"),
            )
            return OrderResult(
                success=True,
                order_id=data.get("id"),
                ticker=ticker,
                action=action,
                quantity=quantity,
                fill_price=fill_price,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] Order failed for %s: %s", ticker, error_body)
            return OrderResult(
                success=False,
                order_id=None,
                ticker=ticker,
                action=action,
                quantity=quantity,
                fill_price=None,
                error=error_body,
            )

    def get_position(self, ticker: str) -> PositionInfo | None:
        """Return current position for ticker, or None if flat."""
        try:
            data = self._get(f"/positions/{ticker.upper()}")
            return PositionInfo(
                ticker=ticker,
                quantity=float(data["qty"]),
                avg_cost=float(data["avg_entry_price"]),
                market_value=float(data["market_value"]),
                unrealized_pnl=float(data["unrealized_pl"]),
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None  # No position
            raise

    def get_all_positions(self) -> list[PositionInfo]:
        """Return all open positions."""
        data = self._get("/positions")
        return [
            PositionInfo(
                ticker=pos["symbol"],
                quantity=float(pos["qty"]),
                avg_cost=float(pos["avg_entry_price"]),
                market_value=float(pos["market_value"]),
                unrealized_pnl=float(pos["unrealized_pl"]),
            )
            for pos in data
        ]

    def get_portfolio_value(self) -> float:
        """Return total portfolio equity (cash + positions)."""
        data = self._get("/account")
        return float(data["equity"])

    def get_cash(self) -> float:
        """Return available cash balance."""
        data = self._get("/account")
        return float(data["cash"])

    def get_account(self) -> dict:
        """Return full account info dict."""
        return self._get("/account")

    def get_order(self, order_id: str) -> dict:
        """Return order details by ID."""
        return self._get(f"/orders/{order_id}")

    def wait_for_fill(self, order_id: str, timeout: int = 30) -> dict:
        """Poll until order is filled or timeout. Returns final order dict."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            order = self.get_order(order_id)
            status = order.get("status")
            if status in {"filled", "partially_filled", "cancelled", "expired", "rejected"}:
                return order
            time.sleep(1)
        return self.get_order(order_id)

    def cancel_all_orders(self, ticker: str | None = None) -> int:
        """Cancel all open orders (or just for a specific ticker). Returns count cancelled."""
        if ticker:
            open_orders = self._get("/orders", params={"status": "open", "symbols": ticker.upper()})
            cancelled = 0
            for order in open_orders:
                resp = self._delete(f"/orders/{order['id']}")
                if resp.status_code in {200, 204}:
                    cancelled += 1
            return cancelled
        else:
            resp = self._delete("/orders")
            if resp.status_code == 207:
                # Multi-status: count successes
                try:
                    return sum(1 for r in resp.json() if r.get("status") == 200)
                except Exception:
                    return 0
            return 0

    def close_position(self, ticker: str) -> dict:
        """Close the entire position for a ticker at market price."""
        resp = self._delete(f"/positions/{ticker.upper()}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def close_all_positions(self) -> list[dict]:
        """Close all open positions at market price."""
        resp = self._delete("/positions?cancel_orders=true")
        if resp.status_code == 207:
            return resp.json()
        return []

