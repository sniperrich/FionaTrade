from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Bar1m


class CapitalConfirmationService:
    """Volume/flow confirmation layer built from local 1m bars."""

    _SHORT_WINDOW_BARS = 30
    _BASELINE_BARS = 120

    def evaluate(
        self,
        session: Session,
        *,
        ticker: str,
        direction: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        direction_u = (direction or "HOLD").upper()
        if direction_u not in {"BUY", "SHORT"}:
            return {
                "flow_score": 50,
                "flow_bucket": "LOW",
                "position_multiplier": 0.60,
                "volume_ratio": 1.0,
                "directional_follow_through": 0.0,
                "relative_strength_vs_spy": 0.0,
                "reason": "non_directional_signal",
            }

        bars = self._load_bars(session, ticker=ticker, as_of=as_of)
        if len(bars) < 40:
            return {
                "flow_score": 50,
                "flow_bucket": "LOW",
                "position_multiplier": 0.60,
                "volume_ratio": 1.0,
                "directional_follow_through": 0.0,
                "relative_strength_vs_spy": 0.0,
                "reason": "insufficient_bar_data",
            }

        recent = bars[: self._SHORT_WINDOW_BARS]
        baseline = bars[self._SHORT_WINDOW_BARS : self._SHORT_WINDOW_BARS + self._BASELINE_BARS]
        if not baseline:
            baseline = bars[self._SHORT_WINDOW_BARS :]

        avg_recent_vol = sum(float(row.volume or 0.0) for row in recent) / max(1, len(recent))
        avg_base_vol = sum(float(row.volume or 0.0) for row in baseline) / max(1, len(baseline))
        volume_ratio = (avg_recent_vol / avg_base_vol) if avg_base_vol > 0 else 1.0

        recent_open = float(recent[-1].open or 0.0)
        recent_close = float(recent[0].close or 0.0)
        ticker_move_pct = ((recent_close - recent_open) / recent_open * 100.0) if recent_open > 0 else 0.0
        directional_follow = ticker_move_pct if direction_u == "BUY" else (-ticker_move_pct)

        spy_move_pct = self._window_move_pct(session, ticker="SPY", as_of=as_of, bars=self._SHORT_WINDOW_BARS)
        relative_strength = ticker_move_pct - spy_move_pct
        aligned_relative = relative_strength if direction_u == "BUY" else (-relative_strength)

        volume_component = self._clamp((volume_ratio / 2.0) * 100.0, 0.0, 100.0)
        direction_component = self._clamp(50.0 + directional_follow * 10.0, 0.0, 100.0)
        relative_component = self._clamp(50.0 + aligned_relative * 8.0, 0.0, 100.0)

        flow_score = int(round(
            0.40 * volume_component
            + 0.35 * direction_component
            + 0.25 * relative_component
        ))
        flow_score = int(self._clamp(flow_score, 0, 100))
        flow_bucket, position_multiplier = self._map_bucket(flow_score)

        return {
            "flow_score": flow_score,
            "flow_bucket": flow_bucket,
            "position_multiplier": position_multiplier,
            "volume_ratio": round(volume_ratio, 3),
            "directional_follow_through": round(directional_follow, 3),
            "relative_strength_vs_spy": round(relative_strength, 3),
            "ticker_move_pct": round(ticker_move_pct, 3),
            "spy_move_pct": round(spy_move_pct, 3),
            "component_scores": {
                "volume": round(volume_component, 2),
                "direction": round(direction_component, 2),
                "relative": round(relative_component, 2),
            },
            "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
        }

    def _load_bars(
        self,
        session: Session,
        *,
        ticker: str,
        as_of: datetime | None,
    ) -> list[Bar1m]:
        stmt = (
            select(Bar1m)
            .where(Bar1m.ticker == ticker.upper())
            .order_by(desc(Bar1m.ts))
            .limit(self._SHORT_WINDOW_BARS + self._BASELINE_BARS + 10)
        )
        if as_of is not None:
            stmt = stmt.where(Bar1m.ts <= as_of)
        return session.execute(stmt).scalars().all()

    def _window_move_pct(
        self,
        session: Session,
        *,
        ticker: str,
        as_of: datetime | None,
        bars: int,
    ) -> float:
        rows = self._load_bars(session, ticker=ticker, as_of=as_of)[: max(2, bars)]
        if len(rows) < 2:
            return 0.0
        open_px = float(rows[-1].open or 0.0)
        close_px = float(rows[0].close or 0.0)
        if open_px <= 0:
            return 0.0
        return (close_px - open_px) / open_px * 100.0

    @staticmethod
    def _map_bucket(score: int) -> tuple[str, float]:
        if score >= 70:
            # Strong flow can slightly scale in, but execution layer still clamps to max position.
            return "HIGH", 1.10
        if score >= 55:
            return "MEDIUM", 0.80
        if score >= 40:
            return "LOW", 0.60
        return "WEAK", 0.35

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))
