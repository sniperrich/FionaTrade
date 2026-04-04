from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.utils import utc_now
from app.db.models import Bar1m, IngestionCursor, Position, Signal
from app.paper_engine.service import NoMarketDataError, PaperEngineService


def test_paper_execute_and_mark_to_market(session, settings):
    now = utc_now()
    signal = Signal(
        event_id=1,
        action="BUY",
        ticker="AAPL",
        confidence=90,
        horizon_min=120,
        reason="test",
        expires_at=now + timedelta(hours=2),
        fallback_used=True,
        status="ACTIVE",
        created_at=now,
    )
    session.add(signal)
    session.add(
        Bar1m(
            ticker="AAPL",
            ts=now + timedelta(minutes=1),
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.0,
            volume=1000,
            source="test",
        )
    )
    session.add(
        Bar1m(
            ticker="AAPL",
            ts=now + timedelta(minutes=2),
            open=110.0,
            high=111.0,
            low=109.0,
            close=110.0,
            volume=1000,
            source="test",
        )
    )
    session.flush()

    result = PaperEngineService(settings).execute(session)
    portfolio = PaperEngineService(settings).portfolio(session)

    assert result.executed == 1
    assert portfolio["nav"] > settings.initial_nav
    assert portfolio["total_value"] == portfolio["nav"]
    assert portfolio["cash"] > 0


def test_daily_circuit_breaker(session, settings):
    now = utc_now()
    session.add(
        Position(
            ticker="AAPL",
            qty=0.0,
            avg_price=0.0,
            realized_pnl=-4000.0,
            unrealized_pnl=0.0,
            last_price=0.0,
        )
    )
    session.add(
        IngestionCursor(
            cursor_key=f"paper_day_nav_{now.date().isoformat()}",
            cursor_value="100000",
        )
    )
    session.flush()

    result = PaperEngineService(settings).execute(session)
    assert result.halted is True


def test_paper_uses_signal_horizon_for_auto_exit(session, settings):
    now = utc_now()
    created_at = now - timedelta(minutes=10)

    signal = Signal(
        event_id=2,
        action="BUY",
        ticker="MSFT",
        confidence=90,
        horizon_min=1,
        reason="short_horizon_test",
        expires_at=now + timedelta(minutes=30),
        fallback_used=True,
        status="ACTIVE",
        created_at=created_at,
    )
    session.add(signal)
    session.add_all(
        [
            Bar1m(
                ticker="MSFT",
                ts=created_at + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
                source="test",
            ),
            Bar1m(
                ticker="MSFT",
                ts=created_at + timedelta(minutes=2),
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1000,
                source="test",
            ),
        ]
    )
    session.flush()

    result = PaperEngineService(settings).execute(session)
    pos = session.query(Position).filter(Position.ticker == "MSFT").one()

    assert result.executed == 1
    assert result.auto_closed == 1
    assert pos.qty == 0.0


def test_paper_execute_rejects_when_no_market_data(session, settings):
    now = utc_now()
    signal = Signal(
        event_id=3,
        action="BUY",
        ticker="NFLX",
        confidence=90,
        horizon_min=120,
        reason="no_market_data_test",
        expires_at=now + timedelta(hours=2),
        fallback_used=True,
        status="ACTIVE",
        created_at=now,
    )
    session.add(signal)
    session.flush()

    result = PaperEngineService(settings).execute(session)
    session.refresh(signal)

    assert result.executed == 0
    assert signal.status == "REJECTED_NO_MARKET_DATA"


def test_latest_price_raises_without_market_data(session, settings):
    with pytest.raises(NoMarketDataError):
        PaperEngineService(settings)._latest_price(session, "NFLX")


def test_target_position_pct_scales_with_confidence_and_horizon(settings):
    service = PaperEngineService(settings)

    low = service._target_position_pct(confidence=50, horizon_min=settings.default_horizon_min)
    high = service._target_position_pct(confidence=95, horizon_min=settings.default_horizon_min)
    long_horizon = service._target_position_pct(confidence=95, horizon_min=settings.default_horizon_min * 2)

    assert low < high <= settings.max_position_pct
    assert long_horizon == high


def test_buy_signal_reverses_short_into_long(session, settings):
    now = utc_now()
    session.add(
        Position(
            ticker="NVDA",
            qty=-5.0,
            avg_price=110.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            last_price=110.0,
        )
    )
    session.add(
        Signal(
            event_id=4,
            action="BUY",
            ticker="NVDA",
            confidence=95,
            horizon_min=settings.default_horizon_min,
            reason="reverse_short",
            expires_at=now + timedelta(hours=2),
            fallback_used=True,
            status="ACTIVE",
            created_at=now,
        )
    )
    session.add_all(
        [
            Bar1m(
                ticker="NVDA",
                ts=now + timedelta(minutes=1),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
                source="test",
            ),
            Bar1m(
                ticker="NVDA",
                ts=now + timedelta(minutes=2),
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.0,
                volume=1000,
                source="test",
            ),
        ]
    )
    session.flush()

    result = PaperEngineService(settings).execute(session)
    pos = session.query(Position).filter(Position.ticker == "NVDA").one()

    assert result.executed == 1
    assert pos.qty > 0
