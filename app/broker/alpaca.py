"""Alpaca Markets broker — paper and live trading via REST API v2.

Supported features:
  • Market, limit, stop, stop-limit orders
  • Bracket orders (entry + take-profit + stop-loss in one atomic order)
  • Trailing stop orders (% or $ trail)
  • OCO orders (one-cancels-other — for adding exits to existing positions)
  • Notional (dollar-amount) orders — buy $X worth of stock
  • Portfolio history (equity curve)
  • Alpaca market clock (live market open/close status)
  • Historical bars via Alpaca Data API (Finnhub-independent)
  • Open orders check before placing duplicates
  • Account activities (trade history)
  • Asset tradability check
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from app.broker.base import AbstractBroker, OrderResult, PositionInfo
from app.core.config import Settings

logger = logging.getLogger(__name__)

_API_VERSION = "v2"
_DATA_BASE_URL = "https://data.alpaca.markets/v2"


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

    # ── Internal HTTP helpers ───────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None, base: str | None = None) -> Any:
        url = f"{base or self._base_url}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._base_url}{path}"
        resp = requests.post(url, headers=self._headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> Any:
        url = f"{self._base_url}{path}"
        resp = requests.patch(url, headers=self._headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self._base_url}{path}"
        resp = requests.delete(url, headers=self._headers, params=params, timeout=15)
        return resp

    # ── AbstractBroker: core order placement ────────────────────────────────

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str = "day",
        extended_hours: bool = False,
    ) -> OrderResult:
        """Place a simple order. action ∈ {BUY, SELL, SHORT, COVER}.

        BUY   → side=buy  (open long)
        SELL  → side=sell (close long)
        SHORT → side=sell (open short — requires margin account)
        COVER → side=buy  (close short)
        """
        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"
        qty_str = str(int(quantity)) if quantity == int(quantity) else str(round(quantity, 6))

        body: dict = {
            "symbol": ticker.upper(),
            "qty": qty_str,
            "side": side,
            "type": order_type.lower(),
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            body["limit_price"] = f"{limit_price:.2f}"
        if stop_price is not None:
            body["stop_price"] = f"{stop_price:.2f}"
        if extended_hours and order_type.lower() == "limit":
            body["extended_hours"] = True

        try:
            data = self._post("/orders", body)
            fill_price = float(data.get("filled_avg_price") or 0) or None
            logger.info(
                "[alpaca] Order: %s %s %s qty=%s id=%s status=%s",
                action, ticker, order_type, qty_str, data.get("id"), data.get("status"),
            )
            return OrderResult(
                success=True, order_id=data.get("id"), ticker=ticker,
                action=action, quantity=quantity, fill_price=fill_price,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] Order failed %s %s: %s", action, ticker, error_body)
            return OrderResult(
                success=False, order_id=None, ticker=ticker,
                action=action, quantity=quantity, fill_price=None, error=error_body,
            )

    def place_bracket_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
        stop_loss_limit_price: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
        time_in_force: str = "gtc",
    ) -> OrderResult:
        """Place a bracket order: entry + take-profit + stop-loss atomically.

        Alpaca manages the take-profit and stop-loss legs automatically.
        Once the entry fills, both exit legs are activated; when one fills,
        the other is cancelled automatically. This is safer than manual
        stop tracking because it survives server restarts.

        Args:
            take_profit_price: Limit price for the take-profit leg.
            stop_loss_price:   Stop trigger price for the stop-loss leg.
            stop_loss_limit_price: If set, stop-loss becomes stop-limit order.
        """
        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"
        qty_str = str(int(quantity)) if quantity == int(quantity) else str(round(quantity, 6))

        body: dict = {
            "symbol": ticker.upper(),
            "qty": qty_str,
            "side": side,
            "type": order_type.lower(),
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss_price:.2f}"},
        }
        if stop_loss_limit_price is not None:
            body["stop_loss"]["limit_price"] = f"{stop_loss_limit_price:.2f}"
        if limit_price is not None and order_type.lower() == "limit":
            body["limit_price"] = f"{limit_price:.2f}"

        try:
            data = self._post("/orders", body)
            fill_price = float(data.get("filled_avg_price") or 0) or None
            logger.info(
                "[alpaca] Bracket order: %s %s qty=%s tp=%.2f sl=%.2f id=%s status=%s",
                action, ticker, qty_str, take_profit_price, stop_loss_price,
                data.get("id"), data.get("status"),
            )
            return OrderResult(
                success=True, order_id=data.get("id"), ticker=ticker,
                action=action, quantity=quantity, fill_price=fill_price,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] Bracket order failed %s %s: %s", action, ticker, error_body)
            return OrderResult(
                success=False, order_id=None, ticker=ticker,
                action=action, quantity=quantity, fill_price=None, error=error_body,
            )

    def place_oco_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
        stop_loss_limit_price: float | None = None,
        time_in_force: str = "gtc",
    ) -> OrderResult:
        """Place an OCO (One-Cancels-Other) exit order for an existing position.

        Use this to add take-profit + stop-loss to a position that's already open
        (entry already filled). Both legs are submitted; when one fills the other
        is automatically cancelled.
        """
        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"
        qty_str = str(int(quantity)) if quantity == int(quantity) else str(round(quantity, 6))

        body: dict = {
            "symbol": ticker.upper(),
            "qty": qty_str,
            "side": side,
            "type": "limit",
            "time_in_force": time_in_force,
            "order_class": "oco",
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss_price:.2f}"},
        }
        if stop_loss_limit_price is not None:
            body["stop_loss"]["limit_price"] = f"{stop_loss_limit_price:.2f}"

        try:
            data = self._post("/orders", body)
            logger.info(
                "[alpaca] OCO order: %s %s qty=%s tp=%.2f sl=%.2f id=%s",
                action, ticker, qty_str, take_profit_price, stop_loss_price, data.get("id"),
            )
            return OrderResult(
                success=True, order_id=data.get("id"), ticker=ticker,
                action=action, quantity=quantity, fill_price=None,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] OCO order failed %s %s: %s", action, ticker, error_body)
            return OrderResult(
                success=False, order_id=None, ticker=ticker,
                action=action, quantity=quantity, fill_price=None, error=error_body,
            )

    def place_trailing_stop_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        trail_percent: float | None = None,
        trail_price: float | None = None,
        time_in_force: str = "gtc",
    ) -> OrderResult:
        """Place a trailing stop order. Exactly one of trail_percent or trail_price must be set.

        Args:
            trail_percent: % below high-water-mark to trigger (e.g., 5.0 = 5%)
            trail_price:   $ below high-water-mark to trigger (e.g., 10.0 = $10)
        """
        if trail_percent is None and trail_price is None:
            raise ValueError("Either trail_percent or trail_price must be set")

        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"
        qty_str = str(int(quantity)) if quantity == int(quantity) else str(round(quantity, 6))

        body: dict = {
            "symbol": ticker.upper(),
            "qty": qty_str,
            "side": side,
            "type": "trailing_stop",
            "time_in_force": time_in_force,
        }
        if trail_percent is not None:
            body["trail_percent"] = str(trail_percent)
        else:
            body["trail_price"] = str(trail_price)

        try:
            data = self._post("/orders", body)
            logger.info(
                "[alpaca] Trailing stop: %s %s qty=%s trail=%s id=%s",
                action, ticker, qty_str,
                f"{trail_percent}%" if trail_percent else f"${trail_price}",
                data.get("id"),
            )
            return OrderResult(
                success=True, order_id=data.get("id"), ticker=ticker,
                action=action, quantity=quantity, fill_price=None,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] Trailing stop failed %s %s: %s", action, ticker, error_body)
            return OrderResult(
                success=False, order_id=None, ticker=ticker,
                action=action, quantity=quantity, fill_price=None, error=error_body,
            )

    def place_notional_order(
        self,
        ticker: str,
        action: str,
        notional: float,
        time_in_force: str = "day",
    ) -> OrderResult:
        """Place a fractional order by dollar notional amount.

        Buy exactly $notional worth of stock (e.g., $500 of AAPL).
        Only market orders are supported for fractional trading.
        """
        action = action.upper()
        side = "buy" if action in {"BUY", "COVER"} else "sell"

        body: dict = {
            "symbol": ticker.upper(),
            "notional": f"{notional:.2f}",
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }

        try:
            data = self._post("/orders", body)
            fill_price = float(data.get("filled_avg_price") or 0) or None
            logger.info(
                "[alpaca] Notional order: %s %s $%.2f id=%s status=%s",
                action, ticker, notional, data.get("id"), data.get("status"),
            )
            return OrderResult(
                success=True, order_id=data.get("id"), ticker=ticker,
                action=action, quantity=notional, fill_price=fill_price,
            )
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            logger.error("[alpaca] Notional order failed %s %s: %s", action, ticker, error_body)
            return OrderResult(
                success=False, order_id=None, ticker=ticker,
                action=action, quantity=notional, fill_price=None, error=error_body,
            )

    def replace_order(
        self,
        order_id: str,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail: float | None = None,
        time_in_force: str | None = None,
    ) -> dict:
        """Replace (modify) an existing open order.

        Only the provided fields are updated. Useful for:
        - Moving a stop-loss price after entry
        - Updating a limit price if the order hasn't filled
        """
        body: dict = {}
        if qty is not None:
            body["qty"] = str(qty)
        if limit_price is not None:
            body["limit_price"] = f"{limit_price:.2f}"
        if stop_price is not None:
            body["stop_price"] = f"{stop_price:.2f}"
        if trail is not None:
            body["trail"] = str(trail)
        if time_in_force is not None:
            body["time_in_force"] = time_in_force
        return self._patch(f"/orders/{order_id}", body)

    # ── Position management ─────────────────────────────────────────────────

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
                return None
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

    def close_position(self, ticker: str) -> dict:
        """Close the entire position for a ticker at market price."""
        resp = self._delete(f"/positions/{ticker.upper()}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def close_all_positions(self) -> list[dict]:
        """Close all open positions at market price and cancel open orders."""
        resp = self._delete("/positions", params={"cancel_orders": "true"})
        if resp.status_code == 207:
            return resp.json()
        return []

    # ── Account & portfolio ─────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Return total portfolio equity (cash + positions)."""
        data = self._get("/account")
        return float(data["equity"])

    def get_cash(self) -> float:
        """Return available cash balance."""
        data = self._get("/account")
        return float(data["cash"])

    def get_account(self) -> dict:
        """Return full account info dict including buying power, equity, P&L etc."""
        return self._get("/account")

    def get_portfolio_history(
        self,
        period: str = "1M",
        timeframe: str = "1D",
        extended_hours: bool = False,
    ) -> dict:
        """Return portfolio equity curve and P&L history.

        Args:
            period: Time period — '1D', '1W', '1M', '3M', '6M', '1A', or 'all'
            timeframe: Bar size — '1Min', '5Min', '15Min', '1H', '1D'

        Returns dict with keys:
            timestamp: list of Unix timestamps
            equity: list of equity values
            profit_loss: list of P&L values
            profit_loss_pct: list of P&L % values
            base_value: portfolio value at start of period
        """
        return self._get(
            "/account/portfolio/history",
            params={
                "period": period,
                "timeframe": timeframe,
                "extended_hours": str(extended_hours).lower(),
            },
        )

    def get_account_activities(
        self,
        activity_type: str | None = None,
        after: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return account activity history (trades, dividends, etc).

        Args:
            activity_type: 'FILL' for trades, 'DIV' for dividends, None for all
            after: ISO 8601 timestamp — return activities after this time
            until: ISO 8601 timestamp — return activities before this time
        """
        params: dict = {"page_size": min(limit, 100)}
        if activity_type:
            params["activity_type"] = activity_type
        if after:
            params["after"] = after
        if until:
            params["until"] = until

        path = f"/account/activities/{activity_type}" if activity_type else "/account/activities"
        return self._get(path, params=params)

    # ── Orders ─────────────────────────────────────────────────────────────

    def get_order(self, order_id: str, nested: bool = False) -> dict:
        """Return order details by ID. Set nested=True to include bracket legs."""
        return self._get(f"/orders/{order_id}", params={"nested": str(nested).lower()})

    def get_open_orders(self, ticker: str | None = None) -> list[dict]:
        """Return all open orders, optionally filtered by ticker.

        Use this to check if a ticker already has a pending order before placing
        another one (avoids duplicate orders across cycles).
        """
        params: dict = {"status": "open", "limit": 100}
        if ticker:
            params["symbols"] = ticker.upper()
        return self._get("/orders", params=params)

    def get_order_history(
        self,
        ticker: str | None = None,
        status: str = "all",
        limit: int = 50,
        after: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        """Return historical orders.

        Args:
            status: 'open', 'closed', or 'all'
            after: ISO 8601 timestamp
            until: ISO 8601 timestamp
        """
        params: dict = {"status": status, "limit": min(limit, 500)}
        if ticker:
            params["symbols"] = ticker.upper()
        if after:
            params["after"] = after
        if until:
            params["until"] = until
        return self._get("/orders", params=params)

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

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order by ID. Returns True if cancelled."""
        resp = self._delete(f"/orders/{order_id}")
        return resp.status_code in {200, 204}

    def cancel_all_orders(self, ticker: str | None = None) -> int:
        """Cancel all open orders (or just for a specific ticker). Returns count cancelled."""
        if ticker:
            open_orders = self.get_open_orders(ticker)
            cancelled = 0
            for order in open_orders:
                if self.cancel_order(order["id"]):
                    cancelled += 1
            return cancelled
        else:
            resp = self._delete("/orders")
            if resp.status_code == 207:
                try:
                    return sum(1 for r in resp.json() if r.get("status") == 200)
                except Exception:
                    return 0
            return 0

    # ── Market data & clock ─────────────────────────────────────────────────

    def get_market_clock(self) -> dict:
        """Return current market clock from Alpaca.

        Returns dict with:
            is_open: bool — whether regular session is active
            next_open: ISO 8601 datetime of next market open
            next_close: ISO 8601 datetime of next market close
            timestamp: current server timestamp
        """
        return self._get("/clock")

    def is_market_open(self) -> bool:
        """Return True if the regular trading session is currently open."""
        try:
            clock = self.get_market_clock()
            return bool(clock.get("is_open", False))
        except Exception as exc:
            logger.warning("[alpaca] Could not check market clock: %s", exc)
            return False

    def get_latest_price(self, ticker: str) -> float | None:
        """Return the latest trade price for a ticker."""
        try:
            resp = requests.get(
                f"{_DATA_BASE_URL}/stocks/{ticker.upper()}/trades/latest",
                headers=self._headers,
                timeout=10,
            )
            resp.raise_for_status()
            return float(resp.json()["trade"]["p"])
        except Exception as exc:
            logger.warning("[alpaca] Could not get latest price for %s: %s", ticker, exc)
            return None

    def get_latest_bar(self, ticker: str) -> dict | None:
        """Return the latest 1-minute bar for a ticker (open, high, low, close, volume)."""
        try:
            resp = requests.get(
                f"{_DATA_BASE_URL}/stocks/{ticker.upper()}/bars/latest",
                headers=self._headers,
                params={"timeframe": "1Min"},
                timeout=10,
            )
            resp.raise_for_status()
            bar = resp.json().get("bar", {})
            return {
                "open": float(bar.get("o", 0)),
                "high": float(bar.get("h", 0)),
                "low":  float(bar.get("l", 0)),
                "close": float(bar.get("c", 0)),
                "volume": float(bar.get("v", 0)),
                "timestamp": bar.get("t"),
            }
        except Exception as exc:
            logger.warning("[alpaca] Could not get latest bar for %s: %s", ticker, exc)
            return None

    def get_bars(
        self,
        ticker: str,
        timeframe: str = "1Min",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        feed: str = "iex",
    ) -> list[dict]:
        """Fetch historical bars from Alpaca Data API.

        Args:
            timeframe: '1Min', '5Min', '15Min', '30Min', '1H', '1D'
            start:     ISO 8601 start time (e.g. '2026-03-20T09:30:00Z')
            end:       ISO 8601 end time
            limit:     max bars to return (max 10000)
            feed:      'iex' (free) or 'sip' (requires subscription)

        Returns list of dicts: {t, o, h, l, c, v, vw, n}
          t=timestamp, o=open, h=high, l=low, c=close, v=volume,
          vw=vwap, n=trade_count
        """
        params: dict = {
            "timeframe": timeframe,
            "limit": min(limit, 10000),
            "feed": feed,
            "adjustment": "raw",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        bars = []
        url = f"{_DATA_BASE_URL}/stocks/{ticker.upper()}/bars"
        try:
            while True:
                resp = requests.get(url, headers=self._headers, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                bars.extend(data.get("bars") or [])
                next_token = data.get("next_page_token")
                if not next_token or len(bars) >= limit:
                    break
                params["page_token"] = next_token
        except Exception as exc:
            logger.warning("[alpaca] get_bars failed %s: %s", ticker, exc)
        return bars[:limit]

    def get_multi_bars(
        self,
        tickers: list[str],
        timeframe: str = "1Min",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        feed: str = "iex",
    ) -> dict[str, list[dict]]:
        """Fetch bars for multiple tickers in a single API call.

        Returns dict mapping ticker → list of bar dicts.
        """
        params: dict = {
            "symbols": ",".join(t.upper() for t in tickers),
            "timeframe": timeframe,
            "limit": min(limit, 10000),
            "feed": feed,
            "adjustment": "raw",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        result: dict[str, list] = {t.upper(): [] for t in tickers}
        try:
            resp = requests.get(
                f"{_DATA_BASE_URL}/stocks/bars",
                headers=self._headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for ticker, bars in (data.get("bars") or {}).items():
                result[ticker.upper()] = bars
        except Exception as exc:
            logger.warning("[alpaca] get_multi_bars failed: %s", exc)
        return result

    def get_latest_quotes(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch latest bid/ask quotes for multiple tickers.

        Returns dict mapping ticker → {ap=ask, bp=bid, as=ask_size, bs=bid_size}
        """
        try:
            resp = requests.get(
                f"{_DATA_BASE_URL}/stocks/quotes/latest",
                headers=self._headers,
                params={"symbols": ",".join(t.upper() for t in tickers)},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("quotes", {})
        except Exception as exc:
            logger.warning("[alpaca] get_latest_quotes failed: %s", exc)
            return {}

    def get_latest_prices(self, tickers: list[str]) -> dict[str, float]:
        """Fetch latest trade prices for multiple tickers in one call.

        Returns dict mapping ticker → price. Missing tickers will be absent.
        """
        try:
            resp = requests.get(
                f"{_DATA_BASE_URL}/stocks/trades/latest",
                headers=self._headers,
                params={"symbols": ",".join(t.upper() for t in tickers)},
                timeout=15,
            )
            resp.raise_for_status()
            trades = resp.json().get("trades", {})
            return {
                ticker: float(trade["p"])
                for ticker, trade in trades.items()
                if "p" in trade
            }
        except Exception as exc:
            logger.warning("[alpaca] get_latest_prices failed: %s", exc)
            return {}

    # ── Asset info ─────────────────────────────────────────────────────────

    def check_asset(self, ticker: str) -> dict:
        """Check if a ticker is tradeable, shortable, and fractionable.

        Returns dict with keys:
            tradable: bool
            shortable: bool
            fractionable: bool
            status: str ('active' or 'inactive')
            easy_to_borrow: bool (relevant for shorting)
        """
        try:
            data = self._get(f"/assets/{ticker.upper()}")
            return {
                "tradable": data.get("tradable", False),
                "shortable": data.get("shortable", False),
                "fractionable": data.get("fractionable", False),
                "status": data.get("status", "unknown"),
                "easy_to_borrow": data.get("easy_to_borrow", False),
                "symbol": data.get("symbol", ticker),
            }
        except Exception as exc:
            logger.warning("[alpaca] check_asset failed for %s: %s", ticker, exc)
            return {"tradable": False, "shortable": False, "fractionable": False,
                    "status": "unknown", "easy_to_borrow": False, "symbol": ticker}

