from __future__ import annotations

from pathlib import Path

import pytest

from app.services.env_settings import EnvSettingsService


def test_env_settings_apply_updates_patch_and_append(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nLIVE_TRADING_TICKERS=AAPL\nENABLE_SEC=false\n", encoding="utf-8")

    svc = EnvSettingsService(env_path=env_file)
    result = svc.apply_updates(
        updates={
            "LIVE_TRADING_TICKERS": "aapl, nvda, aapl",
            "ENABLE_SEC": True,
            "LIVE_CYCLE_INTERVAL_SECONDS": "600",
        }
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert "# comment" in rendered
    assert "LIVE_TRADING_TICKERS=AAPL,NVDA" in rendered
    assert "ENABLE_SEC=true" in rendered
    assert "LIVE_CYCLE_INTERVAL_SECONDS=600" in rendered
    assert result["created"] is False


def test_env_settings_apply_updates_supports_extra_updates(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("POLL_INTERVAL_SECONDS=60\n", encoding="utf-8")

    svc = EnvSettingsService(env_path=env_file)
    svc.apply_updates(
        updates={"POLL_INTERVAL_SECONDS": 120},
        extra_updates_text="\n# keep this\nFINNHUB_API_KEY=abc123\nLLM_MODEL=claude-sonnet-4-5\n",
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert "POLL_INTERVAL_SECONDS=120" in rendered
    assert "FINNHUB_API_KEY=abc123" in rendered
    assert "LLM_MODEL=claude-sonnet-4-5" in rendered


def test_env_settings_apply_updates_validates_extra_format(tmp_path: Path):
    env_file = tmp_path / ".env"
    svc = EnvSettingsService(env_path=env_file)

    with pytest.raises(ValueError, match="must be KEY=VALUE"):
        svc.apply_updates(updates={}, extra_updates_text="BROKEN_LINE")


def test_env_settings_apply_updates_supports_event_driven_and_weights(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("LIVE_EVENT_DRIVEN_MODE=false\nAGENT_WEIGHT_NEWS=0.45\n", encoding="utf-8")
    svc = EnvSettingsService(env_path=env_file)
    svc.apply_updates(
        updates={
            "LIVE_MIN_CONFIDENCE": 72,
            "LIVE_EVENT_DRIVEN_MODE": True,
            "LIVE_FALLBACK_CYCLE_SECONDS": 900,
            "FLOW_CONFIRMATION_ENABLED": "true",
            "AGENT_WEIGHT_NEWS": "0.6",
            "AGENT_WEIGHT_TECHNICALS": 0.2,
            "AGENT_WEIGHT_MACRO": 0.1,
            "AGENT_WEIGHT_FUNDAMENTALS": 0.1,
        }
    )
    rendered = env_file.read_text(encoding="utf-8")
    assert "LIVE_MIN_CONFIDENCE=72" in rendered
    assert "LIVE_EVENT_DRIVEN_MODE=true" in rendered
    assert "LIVE_FALLBACK_CYCLE_SECONDS=900" in rendered
    assert "FLOW_CONFIRMATION_ENABLED=true" in rendered
    assert "AGENT_WEIGHT_NEWS=0.6" in rendered
    assert "AGENT_WEIGHT_TECHNICALS=0.2" in rendered
    assert "AGENT_WEIGHT_MACRO=0.1" in rendered
    assert "AGENT_WEIGHT_FUNDAMENTALS=0.1" in rendered


def test_env_settings_apply_updates_supports_live_sources_and_finnhub_company_news(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    svc = EnvSettingsService(env_path=env_file)
    svc.apply_updates(
        updates={
            "LIVE_ALLOWED_SOURCES": "FINNHUB, SEC, Cnbc, FINNHUB",
            "ENABLE_FINNHUB_COMPANY_NEWS_LIVE": True,
            "FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS": "3",
        }
    )
    rendered = env_file.read_text(encoding="utf-8")
    assert "LIVE_ALLOWED_SOURCES=finnhub,sec,cnbc" in rendered
    assert "ENABLE_FINNHUB_COMPANY_NEWS_LIVE=true" in rendered
    assert "FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS=3" in rendered


def test_env_settings_apply_updates_supports_disable_mode_and_overnight_guard(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    svc = EnvSettingsService(env_path=env_file)
    svc.apply_updates(
        updates={
            "LIVE_DISABLE_DEFAULT_MODE": "flatten_all",
            "LIVE_OVERNIGHT_RISK_ENABLED": True,
            "LIVE_OVERNIGHT_MODE": "alert_only",
            "LIVE_OVERNIGHT_MAX_GROSS_EXPOSURE_PCT": "0.25",
            "LIVE_OVERNIGHT_REBALANCE_MINUTES_BEFORE_CLOSE": 5,
            "LIVE_OVERNIGHT_RUN_WHEN_DISABLED": "true",
        }
    )
    rendered = env_file.read_text(encoding="utf-8")
    assert "LIVE_DISABLE_DEFAULT_MODE=FLATTEN_ALL" in rendered
    assert "LIVE_OVERNIGHT_RISK_ENABLED=true" in rendered
    assert "LIVE_OVERNIGHT_MODE=ALERT_ONLY" in rendered
    assert "LIVE_OVERNIGHT_MAX_GROSS_EXPOSURE_PCT=0.25" in rendered
    assert "LIVE_OVERNIGHT_REBALANCE_MINUTES_BEFORE_CLOSE=5" in rendered
    assert "LIVE_OVERNIGHT_RUN_WHEN_DISABLED=true" in rendered
