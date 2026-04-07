from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.routes import backtest_options, queue_backtest
from app.backtest_engine.agent_backtest import AgentBacktestEngine, AgentBacktestResult, BTCandidateEvent, BTDecision, BTTrade
from app.backtest_engine.service import BacktestEngineService
from app.core.config import DEFAULT_LIVE_ALLOWED_SOURCES
from app.db.models import BacktestTrade, Bar1m, Event, EventEvidence, RawItem, WorkerCommand


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


def _raw_item(*, now: datetime, source: str, ticker: str, title: str, item_hash: str) -> RawItem:
    return RawItem(
        source=source,
        source_tier=1,
        url=f"https://example.com/{item_hash}",
        title=title,
        body=f"{title} detailed body with material company-specific information." * 8,
        published_at=now - timedelta(minutes=5),
        ingested_at=now - timedelta(minutes=4),
        item_hash=item_hash,
        metadata_json={"ticker": ticker, "structured_ticker": True, "matched_tickers": [ticker]},
        processed=False,
    )


def _strong_event(session, *, now: datetime, ticker: str, source: str = "reuters") -> Event:
    raw = _raw_item(
        now=now,
        source=source,
        ticker=ticker,
        title=f"{ticker} wins major contract and raises guidance",
        item_hash=f"hash-{ticker.lower()}-{source}",
    )
    session.add(raw)
    session.flush()
    event = Event(
        event_type="contract_award",
        entities=[ticker],
        tickers=[ticker],
        severity=85,
        event_time=now - timedelta(minutes=5),
        confidence=90,
        validation_status="VALID",
        summary=f"{ticker} wins major contract and raises guidance",
    )
    session.add(event)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source=source,
            source_tier=1,
            captured_at=now - timedelta(minutes=4),
            summary=raw.title,
        )
    )
    session.flush()
    return event


def test_backtest_options_default_to_agent_mode(session, settings) -> None:
    payload = backtest_options(session=session, settings=settings)

    assert payload["defaults"]["engine_mode"] == "agent"
    assert payload["defaults"]["decision_frequency"] == 1
    assert payload["defaults"]["agent_entry_timing"] == "event_time"
    assert payload["defaults"]["tickers"] == list(settings.live_trading_tickers)
    assert payload["defaults"]["intraday_flatten"] is False
    assert payload["defaults"]["sources"] == [source for source in DEFAULT_LIVE_ALLOWED_SOURCES if source not in {"yahoo", "yahoo_finance"}]
    assert payload["defaults"]["use_event_quality_filter"] is settings.backtest_use_event_quality_filter
    assert payload["defaults"]["event_quality_min_score"] == settings.backtest_event_quality_min_score
    assert payload["defaults"]["event_quality_fail_open"] is settings.backtest_event_quality_fail_open


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
    assert command.payload_json["agent_entry_timing"] == "event_time"
    assert command.payload_json["tickers"] == list(settings.live_trading_tickers)
    assert command.payload_json["intraday_flatten"] is False
    assert command.payload_json["sources"] == [source for source in DEFAULT_LIVE_ALLOWED_SOURCES if source not in {"yahoo", "yahoo_finance"}]
    assert command.payload_json["use_event_quality_filter"] is settings.backtest_use_event_quality_filter
    assert command.payload_json["event_quality_min_score"] == settings.backtest_event_quality_min_score
    assert command.payload_json["event_quality_fail_open"] is settings.backtest_event_quality_fail_open


def test_queue_backtest_parses_boolean_strings_and_rejects_bad_numeric_payload(session, settings) -> None:
    response = queue_backtest(
        payload={
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "use_signal_validation": "false",
            "flow_confirmation_enabled": "true",
            "event_quality_min_score": "65",
            "event_quality_fail_open": "false",
        },
        session=session,
        settings=settings,
    )

    command = session.query(WorkerCommand).filter(WorkerCommand.id == response["command_id"]).one()
    assert command.payload_json["use_signal_validation"] is False
    assert command.payload_json["flow_confirmation_enabled"] is True
    assert command.payload_json["event_quality_min_score"] == 65
    assert command.payload_json["event_quality_fail_open"] is False

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


def test_backtest_service_agent_mode_uses_decision_progress_total(session, settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.backtest_engine.service.AgentBacktestEngine._get_trading_days",
        lambda *_args, **_kwargs: [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
    )

    def _fake_run(self, *_args, **_kwargs):
        assert self.progress_callback is not None
        self.progress_callback(1, 3, "Processed 1/3 agent decisions")
        self.progress_callback(2, 3, "Processed 2/3 agent decisions")
        return _fake_agent_result()

    monkeypatch.setattr("app.backtest_engine.service.AgentBacktestEngine.run", _fake_run)

    result = BacktestEngineService(settings).run(
        session,
        params={
            "engine_mode": "agent",
            "start_date": "2026-01-05",
            "end_date": "2026-01-07",
            "tickers": ["AAPL"],
            "decision_frequency": 1,
        },
    )

    run = BacktestEngineService(settings).get_run(session, result.run_id)
    assert run is not None
    assert run.metrics["progress_total"] == 3
    assert run.metrics["phase_total"] == 3
    assert run.metrics["progress_current"] == 3
    assert run.metrics["phase_current"] == 3


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
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AgentBacktestEngine._find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 1,
            "event_type": "contract_award",
            "confidence": 90,
            "summary": "AAPL wins major contract and raises guidance",
            "high_quality_source_count": 1,
            "source_count": 1,
            "sources": ["reuters"],
        },
    )

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
            "agent_entry_timing": "daily_next_open",
        },
    )

    reasons = [trade.reason for trade in result.trades]
    assert "intraday_flatten" in reasons
    assert reasons[-1] == "intraday_flatten"
    buy_trade = next(trade for trade in result.trades if trade.side == "BUY")
    sell_trade = next(trade for trade in result.trades if trade.side == "SELL")
    assert buy_trade.date == date(2026, 1, 6)
    assert sell_trade.date == date(2026, 1, 6)


def test_agent_backtest_event_time_enters_same_session_after_news(session, settings, monkeypatch) -> None:
    market_event_ts = datetime(2026, 1, 5, 15, 15, tzinfo=timezone.utc)  # 10:15 ET

    def _fake_graph_run(_self, _session, _ticker, **_kwargs):
        return {
            "final_action": "BUY",
            "final_position_pct": 0.1,
            "final_reasoning": "ticker-specific catalyst",
            "agent_signals": {
                "news": {"signal": "BUY"},
                "risk_manager": {"signal": "BUY", "metadata": {"approved": True, "max_position_pct": 0.1}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)

    raw = _raw_item(
        now=market_event_ts + timedelta(minutes=5),
        source="reuters",
        ticker="AAPL",
        title="AAPL wins intraday contract and raises guidance",
        item_hash="hash-aapl-event-time",
    )
    raw.published_at = market_event_ts
    raw.ingested_at = market_event_ts + timedelta(minutes=1)
    session.add(raw)
    session.flush()
    event = Event(
        event_type="contract_award",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=90,
        event_time=market_event_ts,
        confidence=95,
        validation_status="WATCH",
        summary="AAPL wins intraday contract and raises guidance",
    )
    session.add(event)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source="reuters",
            source_tier=1,
            captured_at=market_event_ts + timedelta(minutes=1),
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 15, 16, tzinfo=timezone.utc), open=101, high=102, low=100, close=101.5, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=103, high=104, low=102, close=103.5, volume=1000, source="test"),
        ]
    )
    session.flush()
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AgentBacktestEngine._load_event_time_candidates",
        lambda *_args, **_kwargs: [
            BTCandidateEvent(
                ticker="AAPL",
                as_of=market_event_ts,
                event=event,
                trigger_event={
                    "id": int(event.id),
                    "event_type": "contract_award",
                    "confidence": 95,
                    "summary": "AAPL wins intraday contract and raises guidance",
                    "high_quality_source_count": 1,
                    "source_count": 1,
                    "sources": ["reuters"],
                    "quality": "HIGH",
                    "quality_score": 85,
                    "quality_reason": "ticker-specific, fresh, and actionable",
                },
            )
        ],
    )

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "initial_capital": 100_000,
            "max_position_pct": 0.1,
            "slippage_pct": 0.0,
            "intraday_flatten": True,
            "agent_entry_timing": "event_time",
            "sources": ["reuters"],
            "use_event_quality_filter": True,
            "event_quality_min_score": 55,
            "event_quality_fail_open": False,
        },
    )

    buy_trade = next(trade for trade in result.trades if trade.side == "BUY")
    sell_trade = next(trade for trade in result.trades if trade.side == "SELL")
    assert buy_trade.date == date(2026, 1, 5)
    assert sell_trade.date == date(2026, 1, 5)
    assert buy_trade.ts == datetime(2026, 1, 5, 15, 16, tzinfo=timezone.utc)
    assert sell_trade.ts == datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)


def test_agent_backtest_passes_high_quality_sources_to_graph(session, settings, monkeypatch) -> None:
    captured_contexts: list[dict] = []

    def _fake_graph_run(_self, _session, ticker, **kwargs):
        captured_contexts.append(dict(kwargs.get("context") or {}))
        return {
            "final_action": "HOLD",
            "final_position_pct": 0.0,
            "final_reasoning": "no trade",
            "agent_signals": {
                "news": {"signal": "HOLD"},
                "risk_manager": {"signal": "HOLD", "metadata": {"approved": False}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AgentBacktestEngine._find_trigger_event",
        lambda *_args, **_kwargs: {
            "id": 42,
            "event_type": "contract_award",
            "confidence": 90,
            "summary": "AAPL wins major contract and raises guidance",
            "high_quality_source_count": 1,
            "source_count": 1,
            "sources": ["reuters"],
        },
    )

    session.add_all(
        [
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()

    AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["benzinga", "reuters", "yahoo_finance"],
            "agent_entry_timing": "daily_next_open",
        },
    )

    assert captured_contexts
    assert captured_contexts[0]["allowed_sources"] == ["benzinga", "reuters"]
    assert captured_contexts[0]["trigger_event_id"] == 42


def test_agent_backtest_skips_graph_without_strong_tradeable_event(session, settings, monkeypatch) -> None:
    now = datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)
    graph_called = False

    def _fake_graph_run(*_args, **_kwargs):
        nonlocal graph_called
        graph_called = True
        raise AssertionError("graph should not run without strong trigger event")

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_tradeability",
        lambda *_args, **_kwargs: {
            "tradeable": False,
            "strong_sources": 0,
            "hard_event_hits": 0,
            "ticker_specific_hits": 0,
        },
    )

    session.add_all(
        [
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()
    _strong_event(session, now=now, ticker="AAPL")

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["reuters"],
            "agent_entry_timing": "daily_next_open",
        },
    )

    assert graph_called is False
    assert result.total_trades == 0
    assert result.decisions[0].action == "HOLD"
    assert "strong_news_gate" in result.decisions[0].reasoning


def test_agent_backtest_skips_graph_when_llm_quality_gate_rejects_event(session, settings, monkeypatch) -> None:
    now = datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)
    graph_called = False

    def _fake_graph_run(*_args, **_kwargs):
        nonlocal graph_called
        graph_called = True
        raise AssertionError("graph should not run when event quality gate rejects the trigger")

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_tradeability",
        lambda *_args, **_kwargs: {
            "tradeable": True,
            "strong_sources": 1,
            "hard_event_hits": 1,
            "ticker_specific_hits": 1,
        },
    )
    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_event_quality",
        lambda *_args, **_kwargs: {
            "quality": "LOW",
            "quality_score": 25,
            "reason": "mixed or weak evidence",
        },
    )

    session.add_all(
        [
            Bar1m(ticker="SPY", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc), open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()
    _strong_event(session, now=now, ticker="AAPL")

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["reuters"],
            "use_event_quality_filter": True,
            "event_quality_min_score": 55,
            "event_quality_fail_open": False,
            "agent_entry_timing": "daily_next_open",
        },
    )

    assert graph_called is False
    assert result.total_trades == 0
    assert result.decisions[0].action == "HOLD"
    assert "strong_news_gate" in result.decisions[0].reasoning


def test_agent_backtest_llm_quality_gate_allows_watch_event_without_tradeability_rule(session, settings, monkeypatch) -> None:
    now = datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)

    def _fake_graph_run(*_args, **_kwargs):
        return {
            "final_action": "BUY",
            "final_position_pct": 0.1,
            "final_reasoning": "high quality catalyst",
            "agent_signals": {
                "news": {"signal": "BUY"},
                "risk_manager": {"signal": "BUY", "metadata": {"approved": True, "max_position_pct": 0.1}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)

    raw = _raw_item(
        now=now,
        source="reuters",
        ticker="AAPL",
        title="AAPL wins major contract and raises guidance",
        item_hash="hash-aapl-watch",
    )
    session.add(raw)
    session.flush()
    event = Event(
        event_type="contract_award",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=85,
        event_time=now - timedelta(minutes=5),
        confidence=90,
        validation_status="WATCH",
        summary="AAPL wins major contract and raises guidance",
    )
    session.add(event)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source="reuters",
            source_tier=1,
            captured_at=now - timedelta(minutes=4),
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(ticker="SPY", ts=now, open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=now, open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()

    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_event_quality",
        lambda *_args, **_kwargs: {
            "quality": "HIGH",
            "quality_score": 85,
            "reason": "ticker-specific, fresh, and actionable",
        },
    )

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["reuters"],
            "use_event_quality_filter": True,
            "event_quality_min_score": 55,
            "event_quality_fail_open": False,
        },
    )

    assert result.decisions[0].action == "BUY"


def test_agent_backtest_llm_quality_gate_allows_unknown_event(session, settings, monkeypatch) -> None:
    now = datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)

    def _fake_graph_run(*_args, **_kwargs):
        return {
            "final_action": "BUY",
            "final_position_pct": 0.1,
            "final_reasoning": "single-ticker hard catalyst",
            "agent_signals": {
                "news": {"signal": "BUY"},
                "risk_manager": {"signal": "BUY", "metadata": {"approved": True, "max_position_pct": 0.1}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)

    raw = _raw_item(
        now=now,
        source="benzinga",
        ticker="JNJ",
        title="Johnson & Johnson secures permanent J-code for INLEXZO",
        item_hash="hash-jnj-watch-unknown",
    )
    raw.body = "Johnson & Johnson secures a permanent J-code that streamlines reimbursement for INLEXZO." * 4
    session.add(raw)
    session.flush()
    event = Event(
        event_type="unknown",
        entities=["JNJ"],
        tickers=["JNJ"],
        severity=70,
        event_time=now - timedelta(minutes=5),
        confidence=70,
        validation_status="WATCH",
        summary="Johnson & Johnson secures permanent J-code for INLEXZO",
    )
    session.add(event)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source="benzinga",
            source_tier=1,
            captured_at=now - timedelta(minutes=4),
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(ticker="SPY", ts=now, open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="JNJ", ts=now, open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()

    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_event_quality",
        lambda *_args, **_kwargs: {
            "quality": "HIGH",
            "quality_score": 80,
            "reason": "single-ticker hard catalyst",
        },
    )

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["JNJ"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["benzinga"],
            "use_event_quality_filter": True,
            "event_quality_min_score": 55,
            "event_quality_fail_open": False,
            "agent_entry_timing": "daily_next_open",
        },
    )

    assert result.decisions[0].action == "BUY"


def test_agent_backtest_without_quality_filter_accepts_strong_watch_event(session, settings, monkeypatch) -> None:
    now = datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc)

    def _fake_graph_run(*_args, **_kwargs):
        return {
            "final_action": "BUY",
            "final_position_pct": 0.1,
            "final_reasoning": "strong single-source catalyst",
            "agent_signals": {
                "news": {"signal": "BUY"},
                "risk_manager": {"signal": "BUY", "metadata": {"approved": True, "max_position_pct": 0.1}},
            },
        }

    monkeypatch.setattr("app.backtest_engine.agent_backtest.AgentGraph.run", _fake_graph_run)

    raw = _raw_item(
        now=now,
        source="reuters",
        ticker="AAPL",
        title="AAPL wins major contract and raises guidance",
        item_hash="hash-aapl-watch-no-quality",
    )
    session.add(raw)
    session.flush()
    event = Event(
        event_type="contract_award",
        entities=["AAPL"],
        tickers=["AAPL"],
        severity=85,
        event_time=now - timedelta(minutes=5),
        confidence=90,
        validation_status="WATCH",
        summary="AAPL wins major contract and raises guidance",
    )
    session.add(event)
    session.flush()
    session.add(
        EventEvidence(
            event_id=event.id,
            raw_item_id=raw.id,
            url=raw.url,
            source="reuters",
            source_tier=1,
            captured_at=now - timedelta(minutes=4),
            summary=raw.title,
        )
    )
    session.add_all(
        [
            Bar1m(ticker="SPY", ts=now, open=100, high=100, low=100, close=100, volume=1000, source="test"),
            Bar1m(ticker="AAPL", ts=now, open=100, high=101, low=99, close=100, volume=1000, source="test"),
        ]
    )
    session.flush()

    monkeypatch.setattr(
        "app.backtest_engine.agent_backtest.AnalysisService.assess_tradeability",
        lambda *_args, **_kwargs: {
            "tradeable": True,
            "strong_sources": 1,
            "hard_event_hits": 1,
            "ticker_specific_hits": 1,
        },
    )

    result = AgentBacktestEngine(settings).run(
        session,
        params={
            "tickers": ["AAPL"],
            "start_date": "2026-01-05",
            "end_date": "2026-01-05",
            "decision_frequency": 1,
            "sources": ["reuters"],
            "use_event_quality_filter": False,
            "agent_entry_timing": "daily_next_open",
        },
    )

    assert result.decisions[0].action == "BUY"


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
