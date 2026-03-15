# FionaTrade Copilot Instructions

FionaTrade v0.2.0 is an autonomous multi-agent trading system + quantitative research stack.
It combines a 6-agent LangGraph decision pipeline with the original event-driven paper trading infrastructure.
Built with FastAPI + SQLAlchemy + APScheduler on Python 3.11+.

## Commands

```bash
# Run the server
uvicorn app.main:app --reload

# Install (including dev deps)
pip install -e .[dev]

# Run all tests
pytest tests/

# Run a single test
pytest tests/test_agent_graph.py -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

No linter or formatter is configured in this project.

## Architecture

### Dual-Mode Operation

Controlled by `AGENT_MODE_ENABLED` (default `true`):

**Agent Mode (v0.2.0, default)**:
```
IngestionService (news + FRED + fundamentals)
        ↓
AgentGraph (app/agent_graph/graph.py)
  ├─ [parallel] MacroAnalystAgent   → FRED + macro news → LLM
  ├─ [parallel] NewsSentimentAgent  → Event/RawItem DB  → LLM
  ├─ [parallel] FundamentalsAgent   → FundamentalsSnapshot + AnalystRating → LLM
  ├─ [parallel] TechnicalsAgent     → Bar1m → ta library → pure rules (no LLM)
  ├─ [serial]   RiskManagerAgent    → portfolio state + Position table → LLM
  └─ [serial]   PortfolioManagerAgent → all above → LLM → final BUY/SHORT/HOLD
        ↓
AgentRun persisted to DB
        ↓
PaperEngineService (paper execution)
```

**Legacy Mode (`AGENT_MODE_ENABLED=false`)**:
```
IngestionService → NormalizationService → ValidationService
                                                  ↓
                                         SignalEngineService
                                       (AnalysisService + SignalValidator)  [DEPRECATED]
                                                  ↓
                                         PaperEngineService
```

`PipelineOrchestrator` (`app/services/orchestrator.py`) switches between modes.

### Module Responsibilities

| Package | Role |
|---------|------|
| `app/core/` | `Settings` (pydantic-settings, loaded from `.env`), logging setup, SP100 ticker universe |
| `app/db/` | SQLAlchemy models + `db_session()` context manager |
| `app/ingestion/` | Source clients: `sec_client`, `rss_client`, `finnhub_client`, `earnings_release_client`, `fred_client` |
| `app/normalization/` | Clusters raw items → `Event` records; extracts tickers, applies taxonomy |
| `app/validation/` | Deduplication and conflict detection on event clusters |
| `app/tools/` | **Agent tools** — DB-only read functions: `market_data`, `fundamentals`, `news`, `macro` |
| `app/agents/` | 6 agent classes: `base`, `macro_analyst`, `news_sentiment`, `fundamentals`, `technicals`, `risk_manager`, `portfolio_manager` |
| `app/agent_graph/` | `state.py` (TypedDict), `graph.py` (AgentGraph — runs agents, persists AgentRun) |
| `app/broker/` | `base.py` (AbstractBroker), `paper.py` (wraps PaperEngineService), `alpaca.py` (scaffold, NotImplementedError) |
| `app/analysis/` | **DEPRECATED** LLM analysis (`service.py`), kept for backtest compatibility only |
| `app/signal_engine/` | Runs legacy analysis → validation → persists `Signal` rows |
| `app/paper_engine/` | Simulated fills, position tracking, NAV and P&L |
| `app/backtest_engine/` | 3-phase backtest: warmup → parallel LLM batch → serial execution |
| `app/market/` | 1-minute bar backfill: Finnhub primary, yfinance fallback, stooq daily last resort |
| `app/monitoring/` | `HealthAuditService` — source latency and status snapshots |
| `app/api/` | FastAPI JSON endpoints (`/api/*`), including `/api/agent/*` |
| `app/webui/` | Jinja2 template routes including `/agents` page |

### Data Models (app/db/models.py)

**Legacy flow**: `RawItem` → `Event` + `EventEvidence` → `Signal` → `PaperOrder` + `PaperFill` → `Position`

**Agent flow**: `MacroIndicator` + `FundamentalsSnapshot` + `AnalystRating` + `Bar1m` → `AgentRun`

Other models: `EarningsCalendar`, `BacktestRun`, `BacktestTrade`, `IngestionCursor`, `SourceStatus`.

**`AgentRun` key fields**: `ticker`, `macro_output`, `news_output`, `fundamentals_output`, `technicals_output`, `risk_output`, `portfolio_output`, `final_action`, `final_position_pct`, `final_reasoning`, `execution_ms`, `status`.

### Agent System

Each agent extends `BaseAgent` (`app/agents/base.py`):
- `analyze(session, ticker, context) -> AgentSignal`
- `AgentSignal` fields: `agent_name`, `signal` (BUY/SHORT/HOLD/NO_SIGNAL), `confidence` (0–100), `reasoning`, `metadata`, `error`
- Agents **read from DB only** via `app/tools/` — they never call external APIs directly
- LLM calls via `_call_llm(system_prompt, user_prompt, response_format="json")` on `BaseAgent`
- `TechnicalsAgent` is the only agent with no LLM calls — pure `ta` library rules

**Agent weights in PortfolioManager**: technicals=30%, news=25%, fundamentals=25%, macro=20%

**Risk limits (hardcoded in RiskManagerAgent)**:
- Max position: 20% of portfolio (`_MAX_POSITION_PCT`)
- Max daily loss: 3% of initial NAV (`_MAX_DAILY_LOSS_PCT`)
- Minimum consensus: 2 actionable signals (`_MIN_CONSENSUS_COUNT`)

### LLM Integration

- All LLM-using agents call `self._call_llm(system_prompt, user_prompt, response_format="json")`
- Uses the configured endpoint (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`) — OpenAI-compatible
- If LLM is unavailable, agents return `NO_SIGNAL` gracefully
- Retry logic: `LLM_MAX_RETRIES` (default 3), exponential backoff

### Signal Validation Layer (Legacy Mode Only)

Sits between `AnalysisService.event_to_signal()` and execution. Outputs:
- `novelty`: NEW / PARTIALLY_KNOWN / STALE / DUPLICATE
- `review_score`: 0–100
- `execution_recommendation`: APPROVE / DOWNWEIGHT / REJECT / NO_TRADE

In agent mode, risk management is handled by `RiskManagerAgent` instead.

## Key Conventions

### Configuration

All settings live in `app/core/config.py` as a `pydantic-settings` `Settings` class. The singleton is accessed via `get_settings()` (LRU-cached). Settings are loaded from `.env`; copy `.env.example` to `.env` to configure. Services receive `settings` via constructor injection, never via direct import.

Key new settings: `AGENT_MODE_ENABLED`, `AGENT_TICKERS_OVERRIDE`, `AGENT_TECHNICALS_LOOKBACK_BARS`, `FRED_API_KEY`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`.

### Database Session Pattern

Use the `db_session()` context manager for background/script code:
```python
from app.db.database import db_session
with db_session() as session:
    ...  # auto-commit on success, rollback on exception
```

FastAPI routes use `Depends(get_db)` from `app/api/deps.py`.

### Testing Conventions

- `conftest.py` provides two fixtures: `settings` (all external sources disabled, in-memory SQLite, no scheduler) and `session` (in-memory SQLite with `StaticPool` + full schema)
- `StaticPool` is required because `AgentGraph` uses `ThreadPoolExecutor` — all threads must share the same in-memory connection
- Tests never hit real APIs or the filesystem DB
- To test agents: mock `agent._call_llm` to return JSON strings; insert `Bar1m` rows with **recent timestamps** (within the lookback window — use `datetime.utcnow() - timedelta(minutes=N)`)
- Agent graph tests: create `AgentGraph(settings)`, then patch `_call_llm` on each `graph.macro`, `graph.news`, etc.

### Services Are Stateless

All service classes receive `settings` in `__init__` and `session` as a method parameter. They hold no per-request state.

### Source Tiers

- Tier 1: Finnhub company-news (highest signal, ticker-specific)
- Tier 2: RSS (Reuters/AP/Bloomberg/FT/Seeking Alpha), SEC EDGAR, FRED macro
- Tier 3+: lower-priority sources

### Log Files

- `logs/app.log` — main application log (structured entries, includes LLM calls, agent runs)
- `logs/health.log` — health audit log
- `logs/writeout.log` — pipeline tick summaries (JSON blobs via `log_writeout()`)

### Scripts

`scripts/` contains standalone utilities that import from `app/` directly. They use `db_session()` and `get_settings()`. Run from the project root with the virtualenv active.

## Data Sources & API Keys

| Source | Env Var | Notes |
|--------|---------|-------|
| Finnhub | `FINNHUB_API_KEY` | News, 1m bars, earnings, fundamentals, analyst ratings |
| FRED | `FRED_API_KEY` | Macro indicators (CPI/GDP/UNRATE/FEDFUNDS/DGS10/VIX); if unset, macro agent runs without FRED data |
| SEC EDGAR | `SEC_USER_AGENT` | Set to `AppName/version (email)` per SEC policy |
| LLM | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-compatible endpoint; system degrades to NO_SIGNAL if unset |
| Alpaca (scaffold) | `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_BASE_URL` | Live trading scaffold — all methods raise `NotImplementedError` |
| RSS | — | Reuters/AP/Bloomberg/CNBC/FT/Seeking Alpha; configurable via `RSS_SOURCES` |

## New API Endpoints (v0.2.0)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agent/run` | Trigger agent graph for tickers (body: `{"tickers": [...]}`) |
| GET | `/api/agent/runs` | List recent AgentRun records (query: `?ticker=AAPL&limit=20`) |
| GET | `/api/agent/runs/{id}` | Get single AgentRun with full per-agent reasoning |
| POST | `/api/agent/macro/refresh` | Manually trigger FRED indicator refresh |
| POST | `/api/agent/fundamentals/refresh` | Manually trigger fundamentals batch refresh |
| GET | `/agents` | WebUI page showing agent run history with expandable reasoning traces |


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
