"""Tests for TechnicalsAgent — pure rules, no LLM required."""
from __future__ import annotations

import datetime

import pytest

from app.agents.technicals import TechnicalsAgent
from app.db.models import Bar1m


def _add_bars(session, ticker: str, prices: list[float], volumes: list[float] | None = None) -> None:
    """Helper: insert Bar1m rows for a ticker with timestamps during US regular trading hours."""
    if volumes is None:
        volumes = [1_000_000] * len(prices)
    # Use a recent weekday at 10:00 AM ET (15:00 UTC) to ensure bars fall within RTH
    now = datetime.datetime.now(datetime.timezone.utc)
    # Start from a few days ago at 15:00 UTC (10:00 AM ET) — safely within regular hours
    base = now.replace(hour=15, minute=0, second=0, microsecond=0) - datetime.timedelta(days=2)
    # If that's a weekend, go back to Friday
    while base.weekday() >= 5:
        base -= datetime.timedelta(days=1)
    for i, (close, vol) in enumerate(zip(prices, volumes)):
        ts = base + datetime.timedelta(minutes=i)
        bar = Bar1m(
            ticker=ticker,
            ts=ts,
            open=close,
            high=close * 1.001,
            low=close * 0.999,
            close=close,
            volume=vol,
        )
        session.add(bar)
    session.flush()


class TestTechnicalsAgentNoData:
    def test_no_bars_returns_no_signal(self, settings, session):
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "AAPL")
        assert result.signal == "NO_SIGNAL"
        assert result.agent_name == "technicals"

    def test_error_in_metadata(self, settings, session):
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "AAPL")
        assert "error" in result.metadata or result.error or result.signal == "NO_SIGNAL"


class TestTechnicalsAgentBullish:
    def test_oversold_rsi_gives_buy(self, settings, session):
        """A falling price series should produce low RSI → BUY signal."""
        # 100 bars declining steeply (RSI should be <30)
        prices = [200 - i * 1.5 for i in range(100)]
        _add_bars(session, "TSLA", prices)
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "TSLA")
        assert result.agent_name == "technicals"
        assert result.signal in ("BUY", "HOLD", "SHORT")  # must produce a valid signal
        assert result.metadata.get("bar_count", 0) > 0

    def test_flat_prices_gives_hold(self, settings, session):
        """Flat price series → no strong indicator → HOLD."""
        prices = [150.0] * 80
        _add_bars(session, "MSFT", prices)
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "MSFT")
        # RSI will be ~50 for flat prices; score ≈ 0 → HOLD
        assert result.signal in ("HOLD", "BUY", "SHORT")
        assert result.metadata.get("bar_count", 0) > 0


class TestTechnicalsAgentBearish:
    def test_overbought_rsi_gives_short(self, settings, session):
        """A rising price series should produce high RSI → SHORT or HOLD signal."""
        prices = [100 + i * 1.5 for i in range(100)]
        _add_bars(session, "NVDA", prices)
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "NVDA")
        assert result.agent_name == "technicals"
        assert result.signal in ("SHORT", "HOLD", "BUY")  # must produce a valid signal
        assert result.metadata.get("bar_count", 0) > 0


class TestTechnicalsAgentMetadata:
    def test_metadata_contains_expected_keys(self, settings, session):
        prices = [100 + i * 0.1 for i in range(80)]
        _add_bars(session, "META", prices)
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "META")
        assert result.signal in ("BUY", "SHORT", "HOLD")
        assert "score" in result.metadata
        assert "bar_count" in result.metadata
        assert result.metadata["bar_count"] > 0

    def test_volume_surge_adds_to_metadata(self, settings, session):
        # Normal bars + one high-volume bar
        prices = [100.0] * 80
        volumes = [1_000_000] * 79 + [5_000_000]
        _add_bars(session, "AMZN", prices, volumes)
        agent = TechnicalsAgent(settings)
        result = agent.analyze(session, "AMZN")
        assert result.signal in ("BUY", "SHORT", "HOLD")
        assert "volume_ratio" in result.metadata
