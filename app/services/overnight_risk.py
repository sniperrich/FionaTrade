from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.broker.alpaca import AlpacaBroker
from app.broker.base import PositionInfo
from app.core.config import Settings
from app.core.market_hours import et_now, market_session_info
from app.core.utils import utc_now
from app.db.models import LiveTrade
from app.services.runtime_control import CONTROL_OVERNIGHT_RISK_STATE, RuntimeControlService
from app.services.worker_runtime import WorkerRuntimeService

_DISABLE_MODES = {"PAUSE_ONLY", "CANCEL_ORDERS", "FLATTEN_ALL"}
_OVERNIGHT_MODES = {"REDUCE", "FLATTEN", "ALERT_ONLY"}


@dataclass
class ReducePlanItem:
    ticker: str
    action: str
    quantity: int
    price: float
    current_abs_value: float


class OvernightRiskService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.control = RuntimeControlService()
        self.runtime = WorkerRuntimeService()

    @staticmethod
    def normalize_disable_mode(value: str | None, default: str = "CANCEL_ORDERS") -> str:
        mode = str(value or default or "CANCEL_ORDERS").strip().upper()
        return mode if mode in _DISABLE_MODES else "CANCEL_ORDERS"

    @staticmethod
    def normalize_overnight_mode(value: str | None, default: str = "REDUCE") -> str:
        mode = str(value or default or "REDUCE").strip().upper()
        return mode if mode in _OVERNIGHT_MODES else "REDUCE"

    @staticmethod
    def summarize_account(
        account: dict[str, Any] | None,
        positions: list[PositionInfo],
        *,
        open_orders_count: int,
        settings: Settings,
    ) -> dict[str, Any]:
        account = account or {}
        equity = OvernightRiskService._as_float(account.get("equity"))
        cash = OvernightRiskService._as_float(account.get("cash"))
        buying_power = OvernightRiskService._as_float(account.get("buying_power"))
        gross_exposure = sum(abs(OvernightRiskService._as_float(pos.market_value)) for pos in positions)
        net_exposure = sum(OvernightRiskService._as_float(pos.market_value) for pos in positions)
        unrealized_pnl_total = sum(OvernightRiskService._as_float(pos.unrealized_pnl) for pos in positions)
        return {
            "equity": equity,
            "cash": cash,
            "buying_power": buying_power,
            "gross_exposure": gross_exposure,
            "net_exposure": net_exposure,
            "gross_exposure_pct": (gross_exposure / equity) if equity > 0 else None,
            "net_exposure_pct": (net_exposure / equity) if equity > 0 else None,
            "unrealized_pnl_total": unrealized_pnl_total,
            "open_positions_count": len(positions),
            "open_orders_count": int(open_orders_count),
            "risk_source_label": "已有持仓浮盈亏",
            "overnight_guard": {
                "enabled": bool(getattr(settings, "live_overnight_risk_enabled", True)),
                "mode": OvernightRiskService.normalize_overnight_mode(
                    getattr(settings, "live_overnight_mode", "REDUCE"),
                    default="REDUCE",
                ),
                "max_gross_exposure_pct": float(
                    getattr(settings, "live_overnight_max_gross_exposure_pct", 0.25) or 0.25
                ),
                "rebalance_minutes_before_close": int(
                    getattr(settings, "live_overnight_rebalance_minutes_before_close", 5) or 5
                ),
                "run_when_disabled": bool(getattr(settings, "live_overnight_run_when_disabled", True)),
            },
        }

    def fetch_portfolio_snapshot(self, broker: AlpacaBroker | None = None) -> dict[str, Any]:
        broker = broker or AlpacaBroker(self.settings)
        account = broker.get_account()
        positions = broker.get_all_positions()
        open_orders = broker.get_open_orders()
        snapshot = self.summarize_account(
            account,
            positions,
            open_orders_count=len(open_orders),
            settings=self.settings,
        )
        snapshot["positions"] = positions
        snapshot["open_orders"] = open_orders
        return snapshot

    def close_window_state(self, msi: dict[str, Any] | None = None) -> dict[str, Any]:
        msi = msi or market_session_info()
        minutes_before_close = max(
            1,
            int(getattr(self.settings, "live_overnight_rebalance_minutes_before_close", 5) or 5),
        )
        min_until_close = msi.get("min_until_close")
        in_window = bool(msi.get("tradeable")) and min_until_close is not None and 0 <= int(min_until_close) <= minutes_before_close
        return {
            "enabled": bool(getattr(self.settings, "live_overnight_risk_enabled", True)),
            "market_session": msi.get("label"),
            "market_tradeable": bool(msi.get("tradeable")),
            "min_until_close": min_until_close,
            "window_minutes": minutes_before_close,
            "in_close_window": in_window,
        }

    def flatten_all_positions(
        self,
        session: Session,
        *,
        reason: str,
        cycle_id: str,
        event_stage: str,
        requested_by: str,
        cancel_orders: bool = True,
        msi: dict[str, Any] | None = None,
        broker: AlpacaBroker | None = None,
    ) -> dict[str, Any]:
        broker = broker or AlpacaBroker(self.settings)
        msi = msi or market_session_info()
        before = self.fetch_portfolio_snapshot(broker)
        cancelled_orders = 0
        if cancel_orders and before["open_orders"]:
            cancelled_orders = broker.cancel_all_orders()

        submitted = 0
        errors: list[dict[str, Any]] = []
        for position in before["positions"]:
            action = "SELL" if position.quantity > 0 else "COVER"
            qty = int(abs(position.quantity))
            if qty <= 0:
                continue
            try:
                response = broker.close_position(position.ticker)
                submitted += 1
                self._record_live_trade(
                    session=session,
                    cycle_id=cycle_id,
                    ticker=position.ticker,
                    action=action,
                    quantity=qty,
                    target_pct=0.0,
                    order_id=(response or {}).get("id") if isinstance(response, dict) else None,
                    status=((response or {}).get("status") if isinstance(response, dict) else None) or "submitted",
                    market_session=str(msi.get("label") or "closed"),
                    reasoning=reason,
                    error=None,
                )
            except Exception as exc:  # pragma: no cover - exercised via route/service tests
                errors.append({"ticker": position.ticker, "error": str(exc)})
                self._record_live_trade(
                    session=session,
                    cycle_id=cycle_id,
                    ticker=position.ticker,
                    action=action,
                    quantity=qty,
                    target_pct=0.0,
                    order_id=None,
                    status="error",
                    market_session=str(msi.get("label") or "closed"),
                    reasoning=reason,
                    error=str(exc),
                )

        after = self.fetch_portfolio_snapshot(broker)
        result = {
            "cycle_id": cycle_id,
            "requested_by": requested_by,
            "reason": reason,
            "cancelled_orders": cancelled_orders,
            "submitted_orders": submitted,
            "errors": errors,
            "before": self._compact_snapshot(before),
            "after": self._compact_snapshot(after),
        }
        self.runtime.add_event(
            session,
            "live_cycle",
            f"{requested_by} flatten executed",
            level="warn" if errors else "info",
            stage=event_stage,
            agent="risk_control",
            payload=result,
        )
        session.flush()
        return result

    def execute_overnight_guard(
        self,
        session: Session,
        *,
        trigger: str = "scheduled",
        broker: AlpacaBroker | None = None,
    ) -> dict[str, Any]:
        msi = market_session_info()
        window = self.close_window_state(msi)
        mode = self.normalize_overnight_mode(
            getattr(self.settings, "live_overnight_mode", "REDUCE"),
            default="REDUCE",
        )
        result: dict[str, Any] = {
            "trigger": trigger,
            "mode": mode,
            "window": window,
            "executed": False,
            "skipped": True,
        }

        if not window["enabled"]:
            result["reason"] = "overnight_risk_disabled"
            return result

        live_enabled = self.control.get_live_enabled(session, self.settings)
        if not live_enabled and not bool(getattr(self.settings, "live_overnight_run_when_disabled", True)):
            result["reason"] = "live_disabled"
            return result

        if not window["in_close_window"]:
            result["reason"] = "outside_close_window"
            return result

        et_date = et_now().strftime("%Y-%m-%d")
        state = self.control.get(session, CONTROL_OVERNIGHT_RISK_STATE) or {}
        if state.get("executed_et_date") == et_date:
            result["reason"] = "already_executed_today"
            result["state"] = state
            return result

        broker = broker or AlpacaBroker(self.settings)
        before = self.fetch_portfolio_snapshot(broker)
        cycle_id = f"overnight_{mode.lower()}:{et_date}"
        cancelled_orders = 0
        if before["open_orders"]:
            cancelled_orders = broker.cancel_all_orders()
            before["open_orders_count"] = max(0, before["open_orders_count"] - cancelled_orders)

        result.update(
            {
                "skipped": False,
                "executed": True,
                "cycle_id": cycle_id,
                "cancelled_orders": cancelled_orders,
                "before": self._compact_snapshot(before),
            }
        )

        if mode == "ALERT_ONLY":
            result["reason"] = "alert_only"
            self.runtime.add_event(
                session,
                "live_cycle",
                f"Overnight guard alert-only ({et_date})",
                stage="overnight_alert_only",
                agent="risk_control",
                payload=result,
            )
            self._write_state(session, et_date=et_date, mode=mode, result=result)
            session.flush()
            return result

        if not before["positions"]:
            result["reason"] = "no_open_positions"
            result["after"] = self._compact_snapshot(before)
            self.runtime.add_event(
                session,
                "live_cycle",
                f"Overnight guard skipped: no open positions ({et_date})",
                stage="overnight_noop",
                agent="risk_control",
                payload=result,
            )
            self._write_state(session, et_date=et_date, mode=mode, result=result)
            session.flush()
            return result

        if mode == "FLATTEN":
            flatten_reason = (
                "overnight flatten before close; "
                f"gross {before['gross_exposure']:.2f} / equity {before['equity']:.2f}"
            )
            flatten_result = self.flatten_all_positions(
                session,
                reason=flatten_reason,
                cycle_id=cycle_id,
                event_stage="overnight_flatten",
                requested_by="overnight_guard",
                cancel_orders=False,
                msi=msi,
                broker=broker,
            )
            result.update(flatten_result)
            self._write_state(session, et_date=et_date, mode=mode, result=result)
            session.flush()
            return result

        target_pct = max(0.0, float(getattr(self.settings, "live_overnight_max_gross_exposure_pct", 0.25) or 0.25))
        target_gross = max(0.0, before["equity"] * target_pct)
        result["target_gross_exposure"] = target_gross
        if before["gross_exposure"] <= target_gross:
            result["reason"] = "already_within_limit"
            result["after"] = self._compact_snapshot(before)
            self.runtime.add_event(
                session,
                "live_cycle",
                f"Overnight guard no-op: gross already within limit ({et_date})",
                stage="overnight_reduce_noop",
                agent="risk_control",
                payload=result,
            )
            self._write_state(session, et_date=et_date, mode=mode, result=result)
            session.flush()
            return result

        plan = self.build_reduce_plan(before["positions"], target_gross_exposure=target_gross)
        result["plan"] = [self._serialize_plan_item(item) for item in plan]
        if not plan:
            result["reason"] = "no_reduce_plan"
            result["after"] = self._compact_snapshot(before)
            self.runtime.add_event(
                session,
                "live_cycle",
                f"Overnight guard could not build reduce plan ({et_date})",
                level="warn",
                stage="overnight_reduce_error",
                agent="risk_control",
                payload=result,
            )
            self._write_state(session, et_date=et_date, mode=mode, result=result)
            session.flush()
            return result

        submitted_orders = 0
        errors: list[dict[str, Any]] = []
        reason = (
            "overnight reduce before close; "
            f"target gross <= {target_pct:.2%}; before gross={before['gross_exposure']:.2f}"
        )
        for item in plan:
            try:
                order = broker.place_order(
                    ticker=item.ticker,
                    action=item.action,
                    quantity=item.quantity,
                    order_type="market",
                    time_in_force="day",
                )
                status = "submitted" if order.success else "error"
                if order.success:
                    submitted_orders += 1
                else:
                    errors.append({"ticker": item.ticker, "error": order.error or "order_failed"})
                self._record_live_trade(
                    session=session,
                    cycle_id=cycle_id,
                    ticker=item.ticker,
                    action=item.action,
                    quantity=item.quantity,
                    target_pct=0.0,
                    order_id=order.order_id,
                    status=status,
                    market_session=str(msi.get("label") or "closed"),
                    reasoning=reason,
                    error=order.error,
                )
            except Exception as exc:  # pragma: no cover - exercised via service tests
                errors.append({"ticker": item.ticker, "error": str(exc)})
                self._record_live_trade(
                    session=session,
                    cycle_id=cycle_id,
                    ticker=item.ticker,
                    action=item.action,
                    quantity=item.quantity,
                    target_pct=0.0,
                    order_id=None,
                    status="error",
                    market_session=str(msi.get("label") or "closed"),
                    reasoning=reason,
                    error=str(exc),
                )

        after = self.fetch_portfolio_snapshot(broker)
        result.update(
            {
                "reason": "reduced_exposure",
                "submitted_orders": submitted_orders,
                "errors": errors,
                "after": self._compact_snapshot(after),
            }
        )
        self.runtime.add_event(
            session,
            "live_cycle",
            f"Overnight guard reduce executed ({et_date})",
            level="warn" if errors else "info",
            stage="overnight_reduce",
            agent="risk_control",
            payload=result,
        )
        self._write_state(session, et_date=et_date, mode=mode, result=result)
        session.flush()
        return result

    @staticmethod
    def build_reduce_plan(
        positions: list[PositionInfo],
        *,
        target_gross_exposure: float,
    ) -> list[ReducePlanItem]:
        if not positions:
            return []
        gross = sum(abs(OvernightRiskService._as_float(pos.market_value)) for pos in positions)
        if gross <= max(0.0, target_gross_exposure):
            return []

        ratio = max(0.0, min(1.0, target_gross_exposure / gross)) if gross > 0 else 0.0
        plan_by_ticker: dict[str, ReducePlanItem] = {}
        remaining_qty: dict[str, int] = {}
        price_map: dict[str, float] = {}
        current_abs_value_map: dict[str, float] = {}
        remaining_gross = gross

        ordered = sorted(
            positions,
            key=lambda pos: abs(OvernightRiskService._as_float(pos.market_value)),
            reverse=True,
        )
        for position in ordered:
            current_qty = int(abs(position.quantity))
            current_abs_value = abs(OvernightRiskService._as_float(position.market_value))
            price = OvernightRiskService._position_price(position)
            if current_qty <= 0 or price <= 0:
                continue
            target_abs_value = current_abs_value * ratio
            target_qty = min(current_qty, max(0, int(target_abs_value / price)))
            reduce_qty = current_qty - target_qty
            remaining_qty[position.ticker] = current_qty - reduce_qty
            price_map[position.ticker] = price
            current_abs_value_map[position.ticker] = current_abs_value
            if reduce_qty <= 0:
                continue
            plan_by_ticker[position.ticker] = ReducePlanItem(
                ticker=position.ticker,
                action="SELL" if position.quantity > 0 else "COVER",
                quantity=reduce_qty,
                price=price,
                current_abs_value=current_abs_value,
            )
            remaining_gross -= reduce_qty * price

        tolerance = 1e-6
        if remaining_gross > target_gross_exposure + tolerance:
            for position in ordered:
                ticker = position.ticker
                price = price_map.get(ticker, 0.0)
                if price <= 0:
                    continue
                while remaining_gross > target_gross_exposure + tolerance and remaining_qty.get(ticker, 0) > 0:
                    existing = plan_by_ticker.get(ticker)
                    if existing is None:
                        existing = ReducePlanItem(
                            ticker=ticker,
                            action="SELL" if position.quantity > 0 else "COVER",
                            quantity=0,
                            price=price,
                            current_abs_value=current_abs_value_map.get(ticker, abs(OvernightRiskService._as_float(position.market_value))),
                        )
                        plan_by_ticker[ticker] = existing
                    existing.quantity += 1
                    remaining_qty[ticker] -= 1
                    remaining_gross -= price
                    if remaining_qty[ticker] <= 0:
                        break

        return [item for item in plan_by_ticker.values() if item.quantity > 0]

    def _write_state(self, session: Session, *, et_date: str, mode: str, result: dict[str, Any]) -> None:
        self.control.set(
            session,
            CONTROL_OVERNIGHT_RISK_STATE,
            {
                "executed_et_date": et_date,
                "mode": mode,
                "updated_at": utc_now().isoformat(),
                "result": result,
            },
        )

    def _record_live_trade(
        self,
        *,
        session: Session,
        cycle_id: str,
        ticker: str,
        action: str,
        quantity: int,
        target_pct: float,
        order_id: str | None,
        status: str,
        market_session: str,
        reasoning: str,
        error: str | None,
    ) -> None:
        session.add(
            LiveTrade(
                cycle_id=cycle_id,
                ticker=ticker,
                agent_run_id=None,
                action=action,
                quantity=float(quantity),
                target_pct=target_pct,
                order_id=order_id,
                status=status,
                et_time=et_now().strftime("%H:%M ET %a %b %-d"),
                market_session=market_session,
                reasoning=reasoning[:4000] if reasoning else None,
                error=error[:4000] if error else None,
            )
        )

    @staticmethod
    def _serialize_plan_item(item: ReducePlanItem) -> dict[str, Any]:
        return {
            "ticker": item.ticker,
            "action": item.action,
            "quantity": item.quantity,
            "price": item.price,
            "current_abs_value": item.current_abs_value,
        }

    @staticmethod
    def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "equity": snapshot.get("equity"),
            "cash": snapshot.get("cash"),
            "buying_power": snapshot.get("buying_power"),
            "gross_exposure": snapshot.get("gross_exposure"),
            "net_exposure": snapshot.get("net_exposure"),
            "gross_exposure_pct": snapshot.get("gross_exposure_pct"),
            "net_exposure_pct": snapshot.get("net_exposure_pct"),
            "unrealized_pnl_total": snapshot.get("unrealized_pnl_total"),
            "open_positions_count": snapshot.get("open_positions_count"),
            "open_orders_count": snapshot.get("open_orders_count"),
        }

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _position_price(position: PositionInfo) -> float:
        qty = OvernightRiskService._as_float(position.quantity)
        if qty:
            market_value = OvernightRiskService._as_float(position.market_value)
            implied = abs(market_value / qty)
            if implied > 0:
                return implied
        return abs(OvernightRiskService._as_float(position.avg_cost))
