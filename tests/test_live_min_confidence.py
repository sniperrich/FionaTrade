from __future__ import annotations

import pytest

from app.services.live_trading import LiveTradingService


class _DummyGraph:
    def __init__(self, state: dict):
        self._state = dict(state)

    def run(self, _session, _ticker: str, context=None, progress_callback=None):  # noqa: ANN001
        return dict(self._state)


def _open_session() -> dict:
    return {
        "label": "open",
        "tradeable": True,
        "et_time_str": "09:35 ET",
        "context_string": "US market open",
    }


def _base_state(*, action: str = "BUY", confidence: int = 70) -> dict:
    return {
        "final_action": action,
        "final_position_pct": 0.08,
        "final_reasoning": "base reasoning",
        "portfolio_manager_result": {
            "confidence": confidence,
            "signal": action,
            "metadata": {"action": action, "position_pct": 0.08},
        },
    }


def test_live_min_confidence_blocks_directional_orders(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_min_confidence": 75,
            "live_entry_planning_enabled": False,
            "flow_confirmation_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    service._agent_graph = _DummyGraph(_base_state(action="BUY", confidence=62))
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 1,
            "event_type": "earnings_miss",
            "confidence": 88,
            "high_quality_source_count": 2,
        },
    )

    monkeypatch.setattr(
        service,
        "_submit_order_for_action",
        lambda **_kwargs: pytest.fail("_submit_order_for_action should not be called when confidence is blocked"),
    )

    result = service._process_ticker(
        session=session,
        broker=object(),  # broker won't be used due confidence gate
        ticker="AAPL",
        portfolio_value=100_000.0,
        cycle_id="pytest-low-confidence",
        msi=_open_session(),
        dry_run=False,
        run=None,
        fast_path=False,
    )

    assert result["action"] == "HOLD"
    assert result["order_placed"] is False
    assert result["blocked_by_confidence"] is True
    assert result["final_confidence"] == 62
    assert result["live_min_confidence"] == 75


def test_live_min_confidence_allows_order_when_confidence_passes(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_min_confidence": 70,
            "live_entry_planning_enabled": False,
            "flow_confirmation_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    service._agent_graph = _DummyGraph(_base_state(action="BUY", confidence=88))
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 2,
            "event_type": "major_litigation",
            "confidence": 90,
            "high_quality_source_count": 2,
        },
    )

    captured: dict[str, object] = {}

    def _fake_submit_order(**kwargs):
        captured.update(kwargs)
        return {"ticker": kwargs["ticker"], "action": kwargs["desired_action"], "order_placed": True, "order_id": "OID-1"}

    monkeypatch.setattr(service, "_submit_order_for_action", _fake_submit_order)

    result = service._process_ticker(
        session=session,
        broker=object(),
        ticker="AAPL",
        portfolio_value=100_000.0,
        cycle_id="pytest-high-confidence",
        msi=_open_session(),
        dry_run=False,
        run=None,
        fast_path=False,
    )

    assert captured.get("desired_action") == "BUY"
    assert result["order_placed"] is True
    assert result["blocked_by_confidence"] is False
    assert result["final_confidence"] == 88
    assert result["live_min_confidence"] == 70


def test_live_requires_trigger_event_to_trade(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_min_confidence": 50,
            "live_entry_planning_enabled": False,
            "flow_confirmation_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    service._agent_graph = _DummyGraph(_base_state(action="BUY", confidence=88))
    monkeypatch.setattr(service, "_find_trigger_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_submit_order_for_action",
        lambda **_kwargs: pytest.fail("_submit_order_for_action should not be called when trigger event is missing"),
    )

    result = service._process_ticker(
        session=session,
        broker=object(),
        ticker="AAPL",
        portfolio_value=100_000.0,
        cycle_id="pytest-missing-event",
        msi=_open_session(),
        dry_run=False,
        run=None,
        fast_path=False,
    )

    assert result["action"] == "HOLD"
    assert result["blocked_by_missing_event"] is True
    assert result["trigger_event_id"] is None


def test_live_blocks_short_when_news_is_buy_without_dual_hq_confirmation(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_min_confidence": 50,
            "live_entry_planning_enabled": False,
            "flow_confirmation_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    service._agent_graph = _DummyGraph(
        {
            **_base_state(action="SHORT", confidence=91),
            "news_sentiment_result": {"signal": "BUY", "confidence": 72},
        }
    )
    monkeypatch.setattr(
        service,
        "_find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 3,
            "event_type": "guidance_cut",
            "confidence": 88,
            "high_quality_source_count": 1,
        },
    )
    monkeypatch.setattr(
        service,
        "_submit_order_for_action",
        lambda **_kwargs: pytest.fail("_submit_order_for_action should not be called when news conflict gate blocks trade"),
    )

    result = service._process_ticker(
        session=session,
        broker=object(),
        ticker="AAPL",
        portfolio_value=100_000.0,
        cycle_id="pytest-news-conflict",
        msi=_open_session(),
        dry_run=False,
        run=None,
        fast_path=False,
    )

    assert result["action"] == "HOLD"
    assert result["blocked_by_news_conflict"] is True
    assert result["trigger_event_id"] == 3
