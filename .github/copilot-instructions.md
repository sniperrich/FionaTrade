# FionaTrade Copilot Instructions

FionaTrade V1 is an event-driven paper trading research stack: ingest → normalize/cluster → LLM analysis → signal validation → paper execution → backtest. Built with FastAPI + SQLAlchemy + APScheduler on Python 3.11+.

## Commands

```bash
# Run the server
uvicorn app.main:app --reload

# Install (including dev deps)
pip install -e .[dev]

# Run all tests
pytest tests/

# Run a single test
pytest tests/test_signal_validator.py::test_signal_validator_rejects_stale_news -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

No linter or formatter is configured in this project.

## Architecture

### Pipeline Flow

The core loop runs every `POLL_INTERVAL_SECONDS` (default 60s) via APScheduler:

```
IngestionService → NormalizationService → ValidationService
                                                  ↓
                                         SignalEngineService
                                       (AnalysisService + SignalValidator)
                                                  ↓
                                         PaperEngineService
```

`PipelineOrchestrator` (`app/services/orchestrator.py`) wires all stages together. API routes (`/api/ingest/run`, `/api/signals/run`, `/api/paper/execute`) each call into the orchestrator to trigger individual stages on demand.

### Module Responsibilities

| Package | Role |
|---------|------|
| `app/core/` | `Settings` (pydantic-settings, loaded from `.env`), logging setup, SP100 ticker universe |
| `app/db/` | SQLAlchemy models + `db_session()` context manager |
| `app/ingestion/` | One client per source: `sec_client`, `rss_client`, `finnhub_client`, `earnings_release_client` |
| `app/normalization/` | Clusters raw items → `Event` records; extracts tickers, applies taxonomy |
| `app/validation/` | Deduplication and conflict detection on event clusters |
| `app/analysis/` | LLM analysis (`service.py`), event taxonomy (`taxonomy.py`), deterministic `SignalValidator`, rules-based fallback |
| `app/signal_engine/` | Runs analysis → validation → persists `Signal` rows |
| `app/paper_engine/` | Simulated fills, position tracking, NAV and P&L |
| `app/backtest_engine/` | 3-phase backtest: warmup → parallel LLM batch (`ThreadPoolExecutor(8)`) → serial trade execution |
| `app/market/` | 1-minute bar backfill: Finnhub primary, yfinance hourly fallback, stooq daily last resort |
| `app/monitoring/` | `HealthAuditService` — source latency and status snapshots |
| `app/api/` | FastAPI JSON endpoints (`/api/*`) |
| `app/webui/` | Jinja2 template routes for dashboard, news, signals, backtests, paper portfolio |

### Data Models (app/db/models.py)

Key flow: `RawItem` → `Event` + `EventEvidence` → `Signal` → `PaperOrder` + `PaperFill` → `Position`

Other models: `Bar1m` (1-min OHLCV), `EarningsCalendar`, `BacktestRun`, `BacktestTrade`, `IngestionCursor`, `SourceStatus`.

### LLM Integration

- **Classifier**: `gemini-3-flash` — classifies event types for RSS/SEC items that aren't ticker-tagged; Finnhub company-news skips this step
- **Main analysis**: `claude-sonnet-4-5` (via OpenAI-compatible gateway) — reads full article + EPS context + technical signals → emits UP/DOWN/NEUTRAL + horizon + position suggestion
- **Signal Validator**: purely deterministic rules, no LLM — runs after main analysis as an execution quality gate
- When `LLM_BASE_URL` is empty or the LLM call fails, `rules_fallback.py` handles analysis

### Signal Validation Layer

Sits between `AnalysisService.event_to_signal()` and execution. Outputs:
- `novelty`: NEW / PARTIALLY_KNOWN / STALE / DUPLICATE
- `review_score`: 0–100 (execution quality, independent of confidence)
- `execution_recommendation`: APPROVE / DOWNWEIGHT / REJECT / NO_TRADE

Signals with `review_score < VALIDATION_MIN_REVIEW_SCORE` (default 40) are rejected. Controlled by `VALIDATION_*` env vars. To disable in backtests, pass `"use_signal_validation": false` in backtest params.

## Key Conventions

### Configuration

All settings live in `app/core/config.py` as a `pydantic-settings` `Settings` class. The singleton is accessed via `get_settings()` (LRU-cached). Settings are loaded from `.env`; copy `.env.example` to `.env` to configure. Services receive `settings` via constructor injection, never via direct import.

### Database Session Pattern

Use the `db_session()` context manager for background/script code:
```python
from app.db.database import db_session
with db_session() as session:
    ...  # auto-commit on success, rollback on exception
```

FastAPI routes use `Depends(get_db)` from `app/api/deps.py`.

### Testing Conventions

- `conftest.py` provides two fixtures: `settings` (all external sources disabled, in-memory SQLite, no scheduler) and `session` (in-memory SQLite with full schema)
- Tests never hit real APIs or the filesystem DB — always use the in-memory fixtures
- To simulate LLM responses, patch `AnalysisService` or pass pre-built `Signal` objects directly into the backtest/paper engine

### Services Are Stateless

All service classes (`IngestionService`, `SignalEngineService`, etc.) receive `settings` in `__init__` and `session` as a method parameter. They hold no per-request state. Instantiate them fresh per request or reuse within a single pipeline tick.

### Source Tiers

Ingestion sources have tiers (used in deduplication and analysis weighting):
- Tier 1: Finnhub company-news (highest signal, ticker-specific)
- Tier 2: RSS (Bloomberg/CNBC/MarketWatch), SEC EDGAR
- Tier 3+: lower-priority sources

### Log Files

- `logs/app.log` — main application log (structured entries, includes LLM calls, signal validation results)
- `logs/health.log` — health audit log
- `logs/writeout.log` — pipeline tick summaries (JSON blobs via `log_writeout()`)

### Scripts

`scripts/` contains standalone utilities that import from `app/` directly. They use `db_session()` and `get_settings()`. Run from the project root with the virtualenv active.

## Data Sources & API Keys

| Source | Env Var | Notes |
|--------|---------|-------|
| Finnhub | `FINNHUB_API_KEY` | Required for news, 1m bars, earnings; disable with `ENABLE_FINNHUB=false` |
| SEC EDGAR | `SEC_USER_AGENT` | Set to `AppName/version (email)` per SEC policy |
| LLM | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-compatible endpoint; system degrades to rules fallback if unset |
| RSS | — | Bloomberg/CNBC/MarketWatch; configurable via `RSS_SOURCES` |
