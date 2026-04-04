from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import ensure_utc, utc_now
from app.db.models import Bar1m, IngestionCursor, PaperFill, PaperOrder, Position, Signal


@dataclass
class PaperExecutionResult:
    executed: int
    rejected_risk: int
    expired: int
    auto_closed: int
    halted: bool


class NoMarketDataError(RuntimeError):
    """Raised when paper execution cannot resolve a trustworthy market price."""


class PaperEngineService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _upsert_cursor(self, session: Session, key: str, value: str) -> None:
        row = session.execute(select(IngestionCursor).where(IngestionCursor.cursor_key == key)).scalar_one_or_none()
        if row:
            row.cursor_value = value
        else:
            session.add(IngestionCursor(cursor_key=key, cursor_value=value))
        session.flush()

    def _get_cursor(self, session: Session, key: str) -> str | None:
        row = session.execute(select(IngestionCursor).where(IngestionCursor.cursor_key == key)).scalar_one_or_none()
        return row.cursor_value if row else None

    @staticmethod
    def _position_horizon_key(ticker: str) -> str:
        return f"paper_pos_horizon_{ticker.upper()}"

    def _normalize_horizon_min(self, horizon_min: int | None) -> int:
        try:
            value = int(horizon_min) if horizon_min is not None else 0
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else self.settings.default_horizon_min

    def _set_position_horizon(self, session: Session, ticker: str, horizon_min: int | None) -> None:
        self._upsert_cursor(
            session,
            self._position_horizon_key(ticker),
            str(self._normalize_horizon_min(horizon_min)),
        )

    def _get_position_horizon(self, session: Session, ticker: str) -> int:
        raw = self._get_cursor(session, self._position_horizon_key(ticker))
        if raw is None:
            return self.settings.default_horizon_min
        try:
            value = int(raw)
        except ValueError:
            return self.settings.default_horizon_min
        return value if value > 0 else self.settings.default_horizon_min

    def _clear_position_horizon(self, session: Session, ticker: str) -> None:
        row = session.execute(
            select(IngestionCursor).where(IngestionCursor.cursor_key == self._position_horizon_key(ticker))
        ).scalar_one_or_none()
        if row:
            session.delete(row)

    def _fetch_finnhub_bars(self, session: Session, ticker: str, start_ts: int, end_ts: int) -> None:
        if not (self.settings.enable_finnhub and self.settings.finnhub_api_key):
            return

        url = "https://finnhub.io/api/v1/stock/candle"
        params = {
            "symbol": ticker,
            "resolution": "1",
            "from": start_ts,
            "to": end_ts,
            "token": self.settings.finnhub_api_key,
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return

        if data.get("s") != "ok":
            return

        t = data.get("t", [])
        o = data.get("o", [])
        h = data.get("h", [])
        l = data.get("l", [])
        c = data.get("c", [])
        v = data.get("v", [])

        for idx, ts in enumerate(t):
            bar_ts = datetime.fromtimestamp(ts, tz=timezone.utc)
            exists = session.execute(
                select(Bar1m.id).where(and_(Bar1m.ticker == ticker, Bar1m.ts == bar_ts)).limit(1)
            ).first()
            if exists:
                continue
            session.add(
                Bar1m(
                    ticker=ticker,
                    ts=bar_ts,
                    open=float(o[idx]),
                    high=float(h[idx]),
                    low=float(l[idx]),
                    close=float(c[idx]),
                    volume=float(v[idx]),
                    source="finnhub",
                )
            )

    def _next_open(self, session: Session, ticker: str, after_ts) -> tuple:
        after_ts = ensure_utc(after_ts)
        bar = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == ticker, Bar1m.ts > after_ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()

        if bar:
            return bar.ts, bar.open

        start_unix = int(after_ts.timestamp())
        end_unix = int((after_ts + timedelta(hours=12)).timestamp())
        self._fetch_finnhub_bars(session, ticker, start_unix, end_unix)

        bar = session.execute(
            select(Bar1m)
            .where(and_(Bar1m.ticker == ticker, Bar1m.ts > after_ts))
            .order_by(Bar1m.ts.asc())
            .limit(1)
        ).scalar_one_or_none()

        if bar:
            return bar.ts, bar.open

        latest = session.execute(
            select(Bar1m).where(Bar1m.ticker == ticker).order_by(Bar1m.ts.desc()).limit(1)
        ).scalar_one_or_none()
        if latest:
            return latest.ts, latest.close

        raise NoMarketDataError(f"no market data available for {ticker}")

    def _latest_price(self, session: Session, ticker: str) -> float:
        bar = session.execute(
            select(Bar1m).where(Bar1m.ticker == ticker).order_by(Bar1m.ts.desc()).limit(1)
        ).scalar_one_or_none()
        if bar:
            return float(bar.close)

        now = utc_now()
        self._fetch_finnhub_bars(session, ticker, int((now - timedelta(hours=4)).timestamp()), int(now.timestamp()))
        bar = session.execute(
            select(Bar1m).where(Bar1m.ticker == ticker).order_by(Bar1m.ts.desc()).limit(1)
        ).scalar_one_or_none()
        if bar:
            return float(bar.close)
        raise NoMarketDataError(f"no latest price available for {ticker}")

    def _position(self, session: Session, ticker: str) -> Position:
        row = session.execute(select(Position).where(Position.ticker == ticker)).scalar_one_or_none()
        if row:
            return row
        row = Position(ticker=ticker, qty=0.0, avg_price=0.0)
        session.add(row)
        session.flush()
        return row

    def _apply_fill(self, pos: Position, side: str, qty: float, price: float, fill_ts) -> float:
        realized = 0.0

        if side == "BUY":
            if pos.qty < 0:
                close_qty = min(abs(pos.qty), qty)
                realized += close_qty * (pos.avg_price - price)
                remaining = qty - close_qty
                pos.qty += close_qty
                if abs(pos.qty) < 1e-9:
                    pos.qty = 0.0
                    pos.avg_price = 0.0
                if remaining > 0:
                    pos.avg_price = price
                    pos.qty += remaining
            else:
                new_qty = pos.qty + qty
                pos.avg_price = ((pos.avg_price * pos.qty) + (price * qty)) / new_qty if new_qty else 0.0
                pos.qty = new_qty

        elif side == "SHORT":
            if pos.qty > 0:
                close_qty = min(pos.qty, qty)
                realized += close_qty * (price - pos.avg_price)
                remaining = qty - close_qty
                pos.qty -= close_qty
                if abs(pos.qty) < 1e-9:
                    pos.qty = 0.0
                    pos.avg_price = 0.0
                if remaining > 0:
                    pos.avg_price = price
                    pos.qty -= remaining
            else:
                new_abs_qty = abs(pos.qty) + qty
                existing_notional = abs(pos.qty) * pos.avg_price
                pos.avg_price = (existing_notional + qty * price) / new_abs_qty if new_abs_qty else 0.0
                pos.qty -= qty

        elif side == "SELL":
            close_qty = min(max(pos.qty, 0.0), qty)
            realized += close_qty * (price - pos.avg_price)
            pos.qty -= close_qty
            if abs(pos.qty) < 1e-9:
                pos.qty = 0.0
                pos.avg_price = 0.0

        elif side == "COVER":
            close_qty = min(abs(min(pos.qty, 0.0)), qty)
            realized += close_qty * (pos.avg_price - price)
            pos.qty += close_qty
            if abs(pos.qty) < 1e-9:
                pos.qty = 0.0
                pos.avg_price = 0.0

        if pos.qty != 0 and pos.opened_at is None:
            pos.opened_at = fill_ts
        if pos.qty == 0:
            pos.opened_at = None

        pos.updated_at = fill_ts
        pos.realized_pnl += realized
        return realized

    def _mark_to_market(self, session: Session) -> tuple[float, float, float]:
        positions = session.execute(select(Position)).scalars().all()
        realized = sum(p.realized_pnl for p in positions)
        unrealized = 0.0

        for pos in positions:
            if pos.qty == 0:
                pos.unrealized_pnl = 0.0
                continue
            px = self._latest_price(session, pos.ticker)
            pos.last_price = px
            if pos.qty > 0:
                pos.unrealized_pnl = pos.qty * (px - pos.avg_price)
            else:
                pos.unrealized_pnl = abs(pos.qty) * (pos.avg_price - px)
            unrealized += pos.unrealized_pnl

        nav = self.settings.initial_nav + realized + unrealized
        return nav, realized, unrealized

    def _cash_balance(self, session: Session) -> float:
        cash = float(self.settings.initial_nav)
        fills = session.execute(select(PaperFill)).scalars().all()
        for fill in fills:
            notional = float(fill.notional or 0.0)
            fee = float(fill.fee or 0.0)
            side = str(fill.side or "").upper()
            if side in {"BUY", "COVER"}:
                cash -= notional
            elif side in {"SELL", "SHORT"}:
                cash += notional
            cash -= fee
        return cash

    def _gross_exposure(self, session: Session) -> float:
        gross = 0.0
        for pos in session.execute(select(Position)).scalars().all():
            if pos.qty == 0:
                continue
            px = pos.last_price or self._latest_price(session, pos.ticker)
            gross += abs(pos.qty * px)
        return gross

    def _risk_allowed(self, session: Session, ticker: str, side: str, qty: float, price: float, nav: float) -> bool:
        if nav <= 0:
            return False

        pos = self._position(session, ticker)
        current_qty = pos.qty
        delta = 0.0
        if side == "BUY":
            delta = qty
        elif side == "SHORT":
            delta = -qty
        elif side == "SELL":
            delta = -qty
        elif side == "COVER":
            delta = qty

        new_qty = current_qty + delta
        projected_notional = abs(new_qty * price)
        if projected_notional > nav * self.settings.max_position_pct + 1e-6:
            return False

        current_gross = self._gross_exposure(session)
        current_notional = abs(current_qty * price)
        projected_gross = current_gross - current_notional + projected_notional
        if projected_gross > nav * self.settings.max_gross_exposure_pct + 1e-6:
            return False

        return True

    def _slipped_price(self, side: str, base_price: float) -> float:
        slip = self.settings.default_slippage_bps / 10_000.0
        if side in {"BUY", "COVER"}:
            return base_price * (1 + slip)
        return base_price * (1 - slip)

    def _day_halted(self, session: Session, nav: float) -> bool:
        day_key = f"paper_day_nav_{utc_now().date().isoformat()}"
        day_start_raw = self._get_cursor(session, day_key)
        if day_start_raw is None:
            self._upsert_cursor(session, day_key, f"{nav:.6f}")
            return False

        day_start = float(day_start_raw)
        if day_start <= 0:
            return False
        pnl_pct = (nav - day_start) / day_start
        return pnl_pct <= self.settings.daily_loss_limit_pct

    def _close_position(self, session: Session, pos: Position, reason: str) -> bool:
        if pos.qty == 0:
            return False
        px = self._latest_price(session, pos.ticker)
        now = utc_now()
        if pos.qty > 0:
            side = "SELL"
            qty = pos.qty
        else:
            side = "COVER"
            qty = abs(pos.qty)

        order = PaperOrder(
            signal_id=None,
            side=side,
            ticker=pos.ticker,
            qty=qty,
            submitted_at=now,
            status=f"AUTO_{reason}",
        )
        session.add(order)
        session.flush()

        fill_px = self._slipped_price(side, px)
        self._apply_fill(pos, side, qty, fill_px, now)
        session.add(
            PaperFill(
                order_id=order.id,
                side=side,
                ticker=pos.ticker,
                qty=qty,
                submitted_at=now,
                filled_at=now,
                fill_price=fill_px,
                slippage_bps=self.settings.default_slippage_bps,
                fee=0.0,
                notional=qty * fill_px,
            )
        )
        self._clear_position_horizon(session, pos.ticker)
        return True

    def _auto_exit_positions(self, session: Session) -> int:
        closed = 0
        now = utc_now()
        positions = session.execute(select(Position).where(Position.qty != 0)).scalars().all()

        for pos in positions:
            px = self._latest_price(session, pos.ticker)
            if pos.qty > 0:
                ret = (px - pos.avg_price) / pos.avg_price if pos.avg_price else 0.0
            else:
                ret = (pos.avg_price - px) / pos.avg_price if pos.avg_price else 0.0

            opened_at = ensure_utc(pos.opened_at) if pos.opened_at else None
            position_horizon_min = self._get_position_horizon(session, pos.ticker)
            horizon_hit = bool(opened_at and now >= opened_at + timedelta(minutes=position_horizon_min))
            stop_hit = ret <= -self.settings.stop_loss_pct
            take_hit = ret >= self.settings.take_profit_pct

            if horizon_hit:
                if self._close_position(session, pos, "HORIZON"):
                    closed += 1
            elif stop_hit:
                if self._close_position(session, pos, "STOP"):
                    closed += 1
            elif take_hit:
                if self._close_position(session, pos, "TAKE"):
                    closed += 1

        return closed

    def execute(self, session: Session) -> PaperExecutionResult:
        now = utc_now()

        expired = 0
        for signal in session.execute(select(Signal).where(and_(Signal.status == "ACTIVE", Signal.expires_at <= now))).scalars().all():
            signal.status = "EXPIRED"
            expired += 1

        nav, _, _ = self._mark_to_market(session)
        halted = self._day_halted(session, nav)
        if halted:
            return PaperExecutionResult(executed=0, rejected_risk=0, expired=expired, auto_closed=0, halted=True)

        active = session.execute(
            select(Signal).where(and_(Signal.status == "ACTIVE", Signal.expires_at > now)).order_by(Signal.created_at.asc())
        ).scalars().all()

        executed = 0
        rejected = 0

        for signal in active:
            ticker = signal.ticker
            pos = self._position(session, ticker)
            signal_horizon_min = self._normalize_horizon_min(signal.horizon_min)

            try:
                fill_ts, base_price = self._next_open(session, ticker, signal.created_at)
            except NoMarketDataError:
                signal.status = "REJECTED_NO_MARKET_DATA"
                continue

            target_notional = nav * self.settings.max_position_pct
            qty = max(round(target_notional / max(base_price, 0.01), 4), 0.0)
            if qty <= 0:
                signal.status = "REJECTED_SIZE"
                continue

            if signal.action == "BUY":
                side = "COVER" if pos.qty < 0 else "BUY"
                qty = min(abs(pos.qty), qty) if side == "COVER" else qty
            elif signal.action in {"SHORT", "SELL"}:
                side = "SELL" if pos.qty > 0 else "SHORT"
                qty = min(pos.qty, qty) if side == "SELL" else qty
            else:
                signal.status = "SKIPPED"
                continue

            if qty <= 0:
                signal.status = "SKIPPED"
                continue

            if not self._risk_allowed(session, ticker, side, qty, base_price, nav):
                signal.status = "REJECTED_RISK"
                rejected += 1
                continue

            order = PaperOrder(
                signal_id=signal.id,
                side=side,
                ticker=ticker,
                qty=qty,
                submitted_at=now,
                status="SUBMITTED",
            )
            session.add(order)
            session.flush()

            fill_price = self._slipped_price(side, base_price)
            self._apply_fill(pos, side, qty, fill_price, fill_ts)
            if pos.qty == 0:
                self._clear_position_horizon(session, ticker)
            elif side in {"BUY", "SHORT"}:
                self._set_position_horizon(session, ticker, signal_horizon_min)

            session.add(
                PaperFill(
                    order_id=order.id,
                    side=side,
                    ticker=ticker,
                    qty=qty,
                    submitted_at=now,
                    filled_at=fill_ts,
                    fill_price=fill_price,
                    slippage_bps=self.settings.default_slippage_bps,
                    fee=0.0,
                    notional=qty * fill_price,
                )
            )

            signal.status = "EXECUTED"
            signal.executed_at = now
            executed += 1

        auto_closed = self._auto_exit_positions(session)
        self._mark_to_market(session)
        session.flush()

        return PaperExecutionResult(
            executed=executed,
            rejected_risk=rejected,
            expired=expired,
            auto_closed=auto_closed,
            halted=False,
        )

    def portfolio(self, session: Session) -> dict:
        nav, realized, unrealized = self._mark_to_market(session)
        cash = self._cash_balance(session)
        positions = session.execute(select(Position).order_by(Position.ticker.asc())).scalars().all()
        active_orders = session.execute(
            select(PaperOrder).where(PaperOrder.status.in_(["SUBMITTED", "AUTO_HORIZON", "AUTO_STOP", "AUTO_TAKE"]))
        ).scalars().all()

        return {
            "nav": nav,
            "total_value": nav,
            "cash": cash,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "positions": [
                {
                    "ticker": p.ticker,
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                    "last_price": p.last_price,
                    "realized_pnl": p.realized_pnl,
                    "unrealized_pnl": p.unrealized_pnl,
                    "updated_at": p.updated_at,
                }
                for p in positions
            ],
            "active_orders": len(active_orders),
        }
