from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import AgentRun, WorkerRun
from app.services.live_trading import LiveTradingService


def _market_open() -> dict:
    return {
        "label": "open",
        "tradeable": True,
        "et_time_str": "09:35 ET",
        "context_string": "US market open",
    }


def _market_closed() -> dict:
    return {
        "label": "closed",
        "tradeable": False,
        "et_time_str": "18:05 ET",
        "context_string": "US market closed",
    }


def test_event_driven_cycle_skips_without_new_tradeable_event(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL"],
            "live_event_driven_mode": True,
            "live_fallback_cycle_seconds": 600,
            "live_ticker_cooldown_minutes": 0,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_open)
    monkeypatch.setattr(service, "_refresh_bars", lambda _session, _tickers: None)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 0)
    monkeypatch.setattr(service, "_count_new_tradeable_articles", lambda _session, since, tickers, allowed_sources=None: 0)
    monkeypatch.setattr(
        service,
        "_get_last_agent_run_time",
        lambda _session, ticker=None: datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    result = service.run_cycle(session, trigger="pytest")
    assert result["skipped"] is True
    assert result["reason"] == "no_new_tradeable_event"
    assert result["event_driven_mode"] is True

    latest = (
        session.query(WorkerRun)
        .filter(WorkerRun.run_type == "live_cycle")
        .order_by(WorkerRun.id.desc())
        .first()
    )
    assert latest is not None
    assert latest.stage == "skipped_no_tradeable_event"


def test_event_driven_cycle_skips_in_closed_session_too(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL"],
            "live_event_driven_mode": True,
            "live_closed_cycle_seconds": 7200,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_closed)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 0)
    monkeypatch.setattr(service, "_count_new_tradeable_articles", lambda _session, since, tickers, allowed_sources=None: 0)
    monkeypatch.setattr(service, "_count_new_tradeable_by_ticker", lambda _session, since, tickers, allowed_sources=None: {"AAPL": 0})
    monkeypatch.setattr(
        service,
        "_get_last_agent_run_time",
        lambda _session, ticker=None: datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    result = service.run_cycle(session, trigger="pytest")
    assert result["skipped"] is True
    assert result["reason"] == "no_new_tradeable_event"
    assert result["market_session"] == "closed"
    assert result["fallback_cycle_seconds"] == 7200


def test_event_driven_cycle_uses_fast_path_on_fallback_tick(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL"],
            "live_event_driven_mode": True,
            "live_fallback_cycle_seconds": 600,
            "live_ticker_cooldown_minutes": 0,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    captured_fast_path: list[bool] = []

    class DummyBroker:
        def __init__(self, _settings) -> None:
            pass

        def get_portfolio_value(self) -> float:
            return 100_000.0

    def _fake_process(
        _session,
        _broker,
        ticker,
        portfolio_value,
        cycle_id,
        msi,
        dry_run=False,
        run=None,
        fast_path=False,
        allowed_sources=None,
    ):
        captured_fast_path.append(bool(fast_path))
        return {
            "ticker": ticker,
            "action": "HOLD",
            "order_placed": False,
        }

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_open)
    monkeypatch.setattr("app.services.live_trading.AlpacaBroker", DummyBroker)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 0)
    monkeypatch.setattr(service, "_refresh_bars", lambda _session, _tickers: None)
    monkeypatch.setattr(service, "_count_new_tradeable_articles", lambda _session, since, tickers, allowed_sources=None: 0)
    monkeypatch.setattr(
        service,
        "_get_last_agent_run_time",
        lambda _session, ticker=None: datetime.now(timezone.utc) - timedelta(minutes=25),
    )
    monkeypatch.setattr(service, "_process_ticker", _fake_process)

    result = service.run_cycle(session, trigger="pytest")
    assert result["run_mode"] == "fast_path"
    assert captured_fast_path == [True]


def test_ticker_cooldown_skips_unchanged_ticker(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL", "NVDA"],
            "live_event_driven_mode": True,
            "live_open_cycle_seconds": 900,
            "live_ticker_cooldown_minutes": 60,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    processed: list[str] = []

    class DummyBroker:
        def __init__(self, _settings) -> None:
            pass

        def get_portfolio_value(self) -> float:
            return 100_000.0

    def _fake_process(
        _session,
        _broker,
        ticker,
        portfolio_value,
        cycle_id,
        msi,
        dry_run=False,
        run=None,
        fast_path=False,
        allowed_sources=None,
    ):
        processed.append(ticker)
        return {"ticker": ticker, "action": "HOLD", "order_placed": False}

    def _last_run(_session, ticker=None):
        now = datetime.now(timezone.utc)
        if ticker == "NVDA":
            return now - timedelta(minutes=10)
        return now - timedelta(minutes=120)

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_open)
    monkeypatch.setattr("app.services.live_trading.AlpacaBroker", DummyBroker)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 1)
    monkeypatch.setattr(service, "_refresh_bars", lambda _session, _tickers: None)
    monkeypatch.setattr(service, "_count_new_tradeable_articles", lambda _session, since, tickers, allowed_sources=None: 1)
    monkeypatch.setattr(
        service,
        "_count_new_tradeable_by_ticker",
        lambda _session, since, tickers, allowed_sources=None: {"AAPL": 1, "NVDA": 0},
    )
    monkeypatch.setattr(service, "_get_last_agent_run_time", _last_run)
    monkeypatch.setattr(service, "_process_ticker", _fake_process)

    result = service.run_cycle(session, trigger="pytest")
    assert result["run_mode"] == "full_graph"
    assert processed == ["AAPL"]
    skipped = [row for row in result["results"] if row.get("reason") == "ticker_cooldown_no_new_event"]
    assert len(skipped) == 1
    assert skipped[0]["ticker"] == "NVDA"


def test_live_enable_warmup_skips_trading(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL"],
            "live_enable_warmup_minutes": 15,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)

    class DummyBroker:
        def __init__(self, _settings) -> None:
            pass

        def get_portfolio_value(self) -> float:
            return 100_000.0

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_open)
    monkeypatch.setattr("app.services.live_trading.AlpacaBroker", DummyBroker)
    monkeypatch.setattr(service, "_refresh_bars", lambda _session, _tickers: None)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 3)
    monkeypatch.setattr(service, "_count_new_tradeable_articles", lambda _session, since, tickers, allowed_sources=None: 2)
    monkeypatch.setattr(service, "_count_new_tradeable_by_ticker", lambda _session, since, tickers, allowed_sources=None: {"AAPL": 2})
    monkeypatch.setattr(service, "_process_ticker", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not process tickers during warm-up")))

    from app.services.runtime_control import RuntimeControlService

    RuntimeControlService().set_live_enabled(session, live_settings, True, source="pytest")

    result = service.run_cycle(session, trigger="pytest")
    assert result["skipped"] is True
    assert result["reason"] == "live_warmup"
    assert result["warmup_active"] is True
    assert result["warmup_remaining_seconds"] > 0


def test_cached_agent_output_respects_ttl(session, settings) -> None:
    service = LiveTradingService(settings)
    now = datetime.now(timezone.utc)
    session.add(
        AgentRun(
            ticker="AAPL",
            status="COMPLETED",
            created_at=now - timedelta(minutes=20),
            macro_output={"signal": "BUY", "confidence": 70},
        )
    )
    session.flush()

    val_hit, hit = service._get_cached_agent_output(
        session,
        ticker="AAPL",
        field="macro_output",
        ttl_minutes=60,
    )
    assert hit is True
    assert val_hit is not None and val_hit.get("signal") == "BUY"

    val_miss, miss = service._get_cached_agent_output(
        session,
        ticker="AAPL",
        field="macro_output",
        ttl_minutes=5,
    )
    assert miss is False
    assert val_miss is None
