from __future__ import annotations

from scripts.preflight_live import build_preflight_snapshot
from scripts.replay_live_cycle import run_replay
from scripts.run_golden_eval import load_manifest


def test_preflight_snapshot_has_core_fields(session, settings):
    snapshot = build_preflight_snapshot(session, settings, check_broker=False)
    assert snapshot["verdict"] in {"PASS", "WARN", "FAIL"}
    assert "checks" in snapshot
    assert "health" in snapshot
    assert "command_queue" in snapshot
    assert any(row["name"] == "database" for row in snapshot["checks"])


def test_golden_manifest_loads():
    cases = load_manifest()
    assert len(cases) >= 1
    assert all("id" in case for case in cases)
    assert all("params" in case for case in cases)


def test_replay_preview_is_safe_by_default(session):
    payload = run_replay(
        tickers=["AAPL"],
        market_label="closed",
        fast_path=False,
        full_agent_pass=False,
        allowed_sources=None,
        news_since=None,
        portfolio_value=100000.0,
    )
    assert payload["mode"] == "preview"
    assert payload["results"][0]["ticker"] == "AAPL"
    assert payload["results"][0]["mode"] == "preview"
