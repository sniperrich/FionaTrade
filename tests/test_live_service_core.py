from __future__ import annotations

from contextlib import contextmanager

from app.db.models import WorkerRun
from app.services.live_trading import LiveTradingService


def _market_open() -> dict:
    return {
        "label": "open",
        "tradeable": True,
        "et_time_str": "09:35 ET",
        "context_string": "US market open",
    }


def test_run_cycle_aggregates_results_and_completes_run(session, settings, monkeypatch) -> None:
    live_settings = settings.model_copy(
        update={
            "live_trading_tickers": ["AAPL", "MSFT"],
            "live_event_driven_mode": False,
            "live_entry_planning_enabled": False,
        }
    )
    service = LiveTradingService(live_settings)
    processed: list[str] = []

    class _DummyBroker:
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
        return {
            "ticker": ticker,
            "action": "BUY" if ticker == "AAPL" else "HOLD",
            "order_placed": ticker == "AAPL",
            "portfolio_value": portfolio_value,
            "cycle_id": cycle_id,
        }

    monkeypatch.setattr("app.services.live_trading.market_session_info", _market_open)
    monkeypatch.setattr("app.services.live_trading.AlpacaBroker", _DummyBroker)
    monkeypatch.setattr("app.services.live_trading.IngestionService.run", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.services.live_trading.count_new_raw_items", lambda _session, _since: 2)
    monkeypatch.setattr(service, "_refresh_bars", lambda _session, _tickers: {"ok": True, "skipped": False})
    monkeypatch.setattr(service, "_process_ticker", _fake_process)

    @contextmanager
    def _same_session():
        yield session

    monkeypatch.setattr("app.services.live_trading.db_session", _same_session)

    result = service.run_cycle(session, trigger="pytest")

    latest = (
        session.query(WorkerRun)
        .filter(WorkerRun.run_type == "live_cycle")
        .order_by(WorkerRun.id.desc())
        .first()
    )

    assert result["orders_placed"] == 1
    assert result["tickers_processed"] == 2
    assert len(result["results"]) == 2
    assert processed == ["AAPL", "MSFT"]
    assert latest is not None
    assert latest.status == "COMPLETED"
    assert (latest.summary_json or {}).get("orders_placed") == 1
