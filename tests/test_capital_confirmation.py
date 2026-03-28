from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Bar1m
from app.services.capital_confirmation import CapitalConfirmationService


def _seed_bars(
    session,
    *,
    ticker: str,
    count: int,
    start_price: float,
    drift_per_bar: float,
    baseline_volume: float,
    recent_volume: float,
) -> None:
    base_ts = datetime.now(timezone.utc) - timedelta(minutes=count + 5)
    for i in range(count):
        px = start_price + drift_per_bar * i
        vol = recent_volume if i >= count - 30 else baseline_volume
        session.add(
            Bar1m(
                ticker=ticker,
                ts=base_ts + timedelta(minutes=i),
                open=px,
                high=px * 1.002,
                low=px * 0.998,
                close=px + drift_per_bar,
                volume=vol,
                source="pytest",
            )
        )
    session.flush()


def test_capital_confirmation_bucket_mapping_boundaries() -> None:
    svc = CapitalConfirmationService()
    assert svc._map_bucket(85) == ("HIGH", 1.00)
    assert svc._map_bucket(69) == ("MEDIUM", 0.80)
    assert svc._map_bucket(54) == ("LOW", 0.60)
    assert svc._map_bucket(10) == ("WEAK", 0.35)


def test_capital_confirmation_insufficient_data_defaults(session) -> None:
    _seed_bars(
        session,
        ticker="AAPL",
        count=20,
        start_price=100.0,
        drift_per_bar=0.05,
        baseline_volume=100_000,
        recent_volume=200_000,
    )
    svc = CapitalConfirmationService()
    result = svc.evaluate(session, ticker="AAPL", direction="BUY")
    assert result["reason"] == "insufficient_bar_data"
    assert result["flow_score"] == 50
    assert result["flow_bucket"] == "LOW"
    assert abs(float(result["position_multiplier"]) - 0.60) < 1e-9


def test_capital_confirmation_high_flow_for_strong_buy(session) -> None:
    _seed_bars(
        session,
        ticker="AAPL",
        count=160,
        start_price=100.0,
        drift_per_bar=0.20,
        baseline_volume=120_000,
        recent_volume=360_000,
    )
    _seed_bars(
        session,
        ticker="SPY",
        count=160,
        start_price=500.0,
        drift_per_bar=0.04,
        baseline_volume=500_000,
        recent_volume=500_000,
    )
    svc = CapitalConfirmationService()
    result = svc.evaluate(session, ticker="AAPL", direction="BUY")
    assert result["flow_score"] >= 70
    assert result["flow_bucket"] == "HIGH"
    assert abs(float(result["position_multiplier"]) - 1.00) < 1e-9
    assert float(result["volume_ratio"]) > 1.5

