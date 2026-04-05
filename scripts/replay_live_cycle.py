#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.market_hours import market_session_info
from app.db.database import SessionLocal, init_db
from app.services.live_trading import LiveTradingService


class _ReplayBroker:
    def __init__(self, portfolio_value: float) -> None:
        self._portfolio_value = float(portfolio_value)

    def get_portfolio_value(self) -> float:
        return self._portfolio_value


def _forced_market_session(label: str) -> dict[str, Any]:
    normalized = str(label or "").strip().lower()
    if normalized in {"open", "market_open"}:
        return {
            "label": "open",
            "tradeable": True,
            "market_open": True,
            "et_time_str": "09:35 ET",
            "context_string": "Forced market open",
        }
    if normalized == "pre_market":
        return {
            "label": "pre_market",
            "tradeable": False,
            "market_open": False,
            "et_time_str": "08:00 ET",
            "context_string": "Forced pre-market",
        }
    return {
        "label": "closed",
        "tradeable": False,
        "market_open": False,
        "et_time_str": "18:00 ET",
        "context_string": "Forced market closed",
    }


@contextmanager
def _patched_replay_context(service: LiveTradingService, *, news_since: datetime | None):
    original = service._effective_news_since
    if news_since is not None:
        service._effective_news_since = lambda **_kwargs: news_since  # type: ignore[method-assign]
    try:
        yield
    finally:
        service._effective_news_since = original  # type: ignore[method-assign]


def run_replay(
    *,
    tickers: list[str],
    market_label: str,
    fast_path: bool,
    full_agent_pass: bool,
    allowed_sources: set[str] | None,
    news_since: datetime | None,
    portfolio_value: float,
) -> dict[str, Any]:
    settings = get_settings()
    service = LiveTradingService(settings)
    msi = _forced_market_session(market_label) if market_label else market_session_info()
    cycle_id = f"replay-{str(uuid.uuid4())[:8]}"
    broker = _ReplayBroker(portfolio_value)

    session = SessionLocal()
    started_at = datetime.now(timezone.utc)
    try:
        with _patched_replay_context(service, news_since=news_since):
            results = []
            warmup_state = service._warmup_state(session)  # noqa: SLF001 - intentional harness use
            for ticker in tickers:
                last_run_at = service._get_last_agent_run_time(session, ticker=ticker.upper())  # noqa: SLF001
                effective_since = service._effective_news_since(  # noqa: SLF001
                    last_run_at=last_run_at,
                    live_enabled_at=warmup_state.get("enabled_at"),
                )
                trigger_event = service._find_trigger_event(  # noqa: SLF001
                    session,
                    ticker=ticker.upper(),
                    since=news_since or effective_since,
                    allowed_sources=allowed_sources or set(),
                )
                if full_agent_pass:
                    result = service._process_ticker(  # noqa: SLF001 - intentional harness use
                        session,
                        broker,
                        ticker.upper(),
                        portfolio_value,
                        cycle_id,
                        msi,
                        dry_run=True,
                        run=None,
                        fast_path=fast_path,
                        allowed_sources=allowed_sources,
                    )
                else:
                    result = {
                        "ticker": ticker.upper(),
                        "mode": "preview",
                        "market_session": msi.get("label"),
                        "fast_path": fast_path,
                        "allowed_sources": sorted(allowed_sources or set()),
                        "last_agent_run_at": last_run_at.isoformat() if last_run_at else None,
                        "news_since": (news_since or effective_since).isoformat() if (news_since or effective_since) else None,
                        "trigger_event": {
                            "id": trigger_event.get("id"),
                            "event_type": trigger_event.get("event_type"),
                            "published_at": trigger_event.get("published_at"),
                            "source": trigger_event.get("source"),
                            "source_tier": trigger_event.get("source_tier"),
                            "high_quality_source_count": trigger_event.get("high_quality_source_count"),
                            "headline": trigger_event.get("headline"),
                        } if trigger_event else None,
                        "would_run_full_agent_pass": bool(trigger_event) or bool(fast_path),
                        "reason": "trigger_event_found" if trigger_event else ("fast_path_forced" if fast_path else "no_trigger_event"),
                    }
                results.append(result)
                session.rollback()
        return {
            "cycle_id": cycle_id,
            "started_at": started_at.isoformat(),
            "market_session": msi,
            "mode": "full_agent_pass" if full_agent_pass else "preview",
            "portfolio_value": portfolio_value,
            "tickers": [ticker.upper() for ticker in tickers],
            "fast_path": fast_path,
            "news_since": news_since.isoformat() if news_since else None,
            "allowed_sources": sorted(allowed_sources or set()),
            "results": results,
        }
    finally:
        session.close()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe diagnostic replay of a live-cycle ticker pass.")
    parser.add_argument("--tickers", required=True, help="Comma-separated ticker list, e.g. AAPL,NVDA,MSFT")
    parser.add_argument("--market-session", default="closed", choices=["open", "pre_market", "closed"], help="forced market session label")
    parser.add_argument("--fast-path", action="store_true", help="replay with fast-path enabled")
    parser.add_argument("--full-agent-pass", action="store_true", help="run the full agent graph; may invoke external LLM/network calls")
    parser.add_argument("--allowed-sources", default="", help="optional comma-separated source whitelist")
    parser.add_argument("--news-since", default="", help="override news_since ISO timestamp for trigger-event lookup")
    parser.add_argument("--portfolio-value", type=float, default=100000.0, help="synthetic portfolio value used for sizing context")
    args = parser.parse_args()

    init_db()
    tickers = [item.strip().upper() for item in args.tickers.split(",") if item.strip()]
    if not tickers:
        raise SystemExit("no tickers provided")
    allowed_sources = {item.strip().lower() for item in args.allowed_sources.split(",") if item.strip()}
    result = run_replay(
        tickers=tickers,
        market_label=args.market_session,
        fast_path=bool(args.fast_path),
        full_agent_pass=bool(args.full_agent_pass),
        allowed_sources=allowed_sources or None,
        news_since=_parse_dt(args.news_since),
        portfolio_value=float(args.portfolio_value),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
