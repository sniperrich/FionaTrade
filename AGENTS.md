# FionaTrade Agent Harness Guide

This repository is operated as an agent-assisted trading system. Treat this
file as the short operational contract for future agent work.

## Primary reality

- Production database is PostgreSQL.
- SQLite still exists for tests and ad hoc local fixtures only.
- Live trading, worker runtime, and control-plane state are DB-backed.
- All persisted timestamps should be treated as UTC.
- Human-facing operational notes in `memory.md` use `Asia/Shanghai (UTC+8)`.

## Core system boundaries

- `app/services/live_trading.py` is the live execution orchestrator.
- `app/agent_graph/graph.py` is the multi-agent decision pipeline.
- `app/backtest_engine/` is offline research and replay.
- `app/services/worker_runtime.py` and `app/services/runtime_control.py` are the
  control-plane backbone.
- `app/broker/alpaca.py` and `app/broker/paper.py` are execution adapters.

Do not blur these boundaries casually. In particular:

- Agents should read data through `app/tools/` wherever possible.
- Runtime/event writes must not pollute the main trading transaction.
- Backtest changes should preserve comparability with live execution.

## Harness-first expectations

When changing logic in live, backtest, brokers, or worker/runtime:

1. Prefer adding a deterministic test before or alongside the change.
2. Keep replay/debug tooling safe by default:
   - analysis-only
   - no live enable toggles
   - no persistent writes unless explicitly requested
3. Preserve a clear validation path:
   - unit/regression tests
   - PostgreSQL smoke when DB-specific behavior changes

## Required validation by area

- Live trading / worker / runtime:
  - `tests/test_live_event_driven.py`
  - `tests/test_live_guardrails.py`
  - `tests/test_live_service_core.py`
  - `tests/test_worker_control_plane.py`

- Brokers / paper execution:
  - `tests/test_brokers.py`
  - `tests/test_paper_engine.py`

- Agent graph / backtest parity:
  - `tests/test_base_agent.py`
  - `tests/test_backtest_agent_mode.py`
  - `tests/test_backtest_llm_mode.py`

## Operational rules

- Do not touch unrelated dirty files unless explicitly asked.
- Do not assume UI green status means execution is healthy; check terminal
  metrics such as:
  - `agent_runs_24h`
  - `live_trades_24h`
  - latest `worker_runs`
  - latest runtime events
- Never reopen unauthenticated control-plane access.

## Repo memory

- `memory.md` is a lightweight harness artifact:
  - short operational memory
  - server/runtime findings
  - UTC+8 dated notes
- `handoff.md` is the higher-level implementation/change log.
- `README.md` is the current operator/developer path, not the full incident log.

## Phase 1 harness assets

- `scripts/preflight_live.py`
- `scripts/replay_live_cycle.py`
- `scripts/run_golden_eval.py`
- `tests/golden/manifest.json`

If these drift from real system behavior, update them with the same priority as
the feature change that caused the drift.
