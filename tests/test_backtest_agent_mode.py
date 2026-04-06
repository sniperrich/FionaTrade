from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.routes import backtest_options, queue_backtest
from app.backtest_engine.agent_backtest import AgentBacktestEngine, AgentBacktestResult, BTDecision, BTTrade
from app.backtest_engine.service import BacktestEngineService
from app.db.models import BacktestTrade, Bar1m, WorkerCommand


def _fake_agent_result() -> AgentBacktestResult:
    return AgentBacktestResult(
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 6),
        tickers=["AAPL"],
        initial_capital=100_000.0,
        final_equity=101_000.0,
        total_return_pct=1.0,
        max_drawdown_pct=0.5,
        total_trades=2,
        winning_trades=1,
        losing_trades=0,
        stop_losses=0,
        equity_curve=[
            {"date": "2026-01-05", "equity": 100_000.0},
            {"date": "2026-01-06", "equity": 101_000.0},
        ],
        trades=[
            BTTrade(
                date=date(2026, 1, 5),
                ticker="AAPL",
                side="BUY",
                shares=10.0,
                price=100.0,
                notional=1_000.0,
                reason="agent_buy_5%",
            ),
            BTTrade(
                date=date(2026, 1, 6),
                ticker="AAPL",
                side="SELL",
                shares=10.0,
                price=110.0,
                notional=1_100.0,
                reason="agent_sell",
            ),
        ],
        decisions=[
            BTDecision(
                date=date(2026, 1, 5),
                ticker="AAPL",
                action="BUY",
                position_pct=0.05,
                reasoning="positive catalyst",
                agent_signals={"news": "BUY", "risk": "BUY"},
            )
        ],
        errors=[],
    )


def test_backtest_options_default_to_agent_mode(session, settings) -> None:
    payload = backtest_options(session=session, settings=settings)

    assert payload["defaults"]["engine_mode"] == "agent"
    assert payload["defaults"]["decision_frequency"] == 1
    assert payload["defaults"]["tickers"] == list(settings.live_trading_tickers)
    assert payload["defaults"]["intraday_flatten"] is False


def test_queue_backtest_defaults_to_agent_mode(session, settings) -> None:
    response = queue_backtest(
        payload={"start_date": "2026-01-01", "end_date": "2026-02-01"},
        session=session,
        settings=settings,
    )

    assert response["queued"] is True
    command = session.query(WorkerCommand).order_by(WorkerCommand.id.desc()).first()
    assert command is not None
    assert command.payload_json["engine_mode"] == "agent"
    assert command.payload_json["tickers"] == list(settings.live_trading_tickers)
    assert command.payload_json["intraday_flatten"] is False


def test_queue_backtest_parses_boolean_strings_and_rejects_bad_numeric_payload(session, settings) -> None:
    response = queue_backtest(
        payload={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "use_signal_validation": "false",
            "flow_confirmation_enabled": "true",
        },
        session=session,
        settings=settings,
    )

    command = session.query(WorkerCommand).filter(WorkerCommand.id == response["command_id"]).one()
    assert command.payload_json["use_signal_validation"] is False
    assert command.payload_json["flow_confirmation_enabled"] is True

    with pytest.raises(HTTPException) as excinfo:
        queue_backtest(
            payload={
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "initial_capital": "abc",
            },
            session=session,
            settings=settings,
        )

    assert excinfo.value.status_code == 400
    assert "initial_capital must be a number" in str(excinfo.value.detail)


def test_backtest_service_agent_mode_persists_live_like_run(session, settings, monkeypatch) -> None:
    monkeypatch.setattr("app.backtest_engine.service.AgentBacktestEngine.run", lambda *_args, **_kwargs: _fake_agent_result())

    result = BacktestEngineService(settings).run(
        session,
        params={
            "engine_mode": "agent",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "tickers": ["AAPL"],
            "decision_frequency": 1,
        },
    )

    run = BacktestEngineService(settings).get_run(session, result.run_id)
    assert run is not None
    assert result.status == "DONE"
    assert run.params["engine_mode"] == "agent"
    assert run.metrics["engine_mode"] == "agent"
    assert run.metrics["mode_label"] == "AGENT"
    assert run.metrics["events_considered"] == 1
    assert run.metrics["profit_factor"] is None
    assert run.metrics["trades"] == 1
    assert len(run.trade_log) == 1
    assert run.trade_log[0]["ticker"] == "AAPL"
    assert run.trade_log[0]["side"] == "LONG"
    assert session.query(BacktestTrade).filter(BacktestTrade.run_id == run.id).count() == 1


def test_agent_backtest_intraday_flatten_closes_same_day(session, settings, monkeypatch) -> None:
    def _fake_graph_run(_self, _session, ticker, **_kwargs):
        return {
            "final_action": "BUY" if ticker == "AAPL" else "HOLD",
            "final_position_pct": 0.1,
            "final_reasoning": "positive catalyst",
            "agent_signals": {
                "news": {"signal": "BUY"},
                "risk_manager": {"signal": "BUY", "metadata": {"approved": True, "max_position_pct": 0.1}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)

    session.add_all(
        [
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 6, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=101, low=99, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc), open=101, high=102, low=100, close=101, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 6, 20, 59, tzinfo=timezone.utc), open=102, high=103, low=101, close=102, volume=1000, source="test"),
        ]
    )
    session.flush()

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "decision_frequency": 1,
            "initial_capital": 100_000,
            "max_position_pct": 0.1,
            "slippage_pct": 0.0,
            "intraday_flatten": True,
        },
    )

    reasons = [trade.reason for trade in result.trades]
    assert "intraday_flatten" in reasons
    assert reasons[-1] == "intraday_flatten"


def test_daily_realized_pnl_uses_closed_trade_pnl_not_cash_flow(settings) -> None:
    trades = [
        BTTrade(date=date(2026, 1, 5), ticker="AAPL", side="BUY", shares=10.0, price=100.0, notional=1000.0, reason="open"),
        BTTrade(date=date(2026, 1, 5), ticker="AAPL", side="SELL", shares=10.0, price=103.0, notional=1030.0, reason="close"),
        BTTrade(date=date(2026, 1, 5), ticker="MSFT", side="SHORT", shares=5.0, price=200.0, notional=1000.0, reason="open"),
        BTTrade(date=date(2026, 1, 5), ticker="MSFT", side="COVER", shares=5.0, price=190.0, notional=950.0, reason="close"),
    ]

    pnl = AgentBacktestEngine(settings)._compute_daily_realized_pnl(
        trades,
        target_day=date(2026, 1, 5),
        ticker="AAPL",
    )
    assert pnl == 30.0

    total_pnl = AgentBacktestEngine(settings)._compute_daily_realized_pnl(
        trades,
        target_day=date(2026, 1, 5),
    )
    assert total_pnl == 80.0
