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
