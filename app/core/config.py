from __future__ import annotations

from functools import lru_cache
import json
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.universe import SP100_TICKERS

DEFAULT_LIVE_TICKERS = [
    "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL",
    "META", "TSLA", "JPM", "XOM", "UNH",
    "JNJ", "PG", "HD", "AVGO", "BAC",
]

DEFAULT_LIVE_ALLOWED_SOURCES = [
    "benzinga",
    "reuters",
    "cnbc",
    "earnings_release",
    "sec",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "FionaTrade"
    env: str = "dev"
    database_url: str = "sqlite:///./fionatrade.db"
    sqlite_busy_timeout_seconds: float = 30.0
    control_api_key: str = ""
    control_api_localhost_bypass: bool = True

    poll_interval_seconds: int = 60
    enable_scheduler: bool = True
    enable_health_audit: bool = True
    health_check_interval_seconds: int = 300

    log_level: str = "INFO"
    log_dir: str = "logs"
    market_backfill_allow_stooq_fallback: bool = True

    enable_sec: bool = True
    enable_rss: bool = True
    enable_finnhub: bool = True
    enable_earnings_release_source: bool = True

    sec_user_agent: str = "FionaTrade/0.1 (your-email@example.com)"
    finnhub_api_key: str = ""

    llm_provider: str = "openai-compatible"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_classifier_model: str = "gemini-3-flash"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_retry_backoff_seconds: float = 1.5
    llm_retry_backoff_multiplier: float = 1.8
    llm_retry_max_delay_seconds: float = 12.0
    # Merge system prompt into user message (for Kiro-routed models that reject role overrides)
    llm_merge_system_prompt: bool = True
    sec_summary_model: str = "gemini-3-flash"
    sec_summary_max_chars: int = 1000
    # Ignore SEC filings older than this age during incremental ingestion.
    sec_recent_max_age_days: int = 14
    event_tradeability_filter_enabled: bool = True
    event_tradeability_min_score: int = 55
    normalization_merge_window_min: int = 0
    enable_term_management: bool = True
    term_short_horizon_min: int = 60
    term_mid_horizon_min: int = 240
    term_long_horizon_min: int = 1440

    initial_nav: float = 100_000.0
    max_position_pct: float = 0.15
    max_gross_exposure_pct: float = 1.00
    daily_loss_limit_pct: float = -0.03
    default_slippage_bps: float = 4.0
    short_borrow_apr: float = 0.03
    default_horizon_min: int = 120
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    backtest_hard_stops: bool = True
    backtest_risk_sizing: bool = True
    backtest_risk_per_trade_pct: float = 0.002
    backtest_daily_circuit_breaker: bool = True
    backtest_enable_term_horizon: bool = False
    backtest_entry_window_min: int = 120
    backtest_regime_risk_adjust: bool = True
    backtest_regime_bull_risk_multiplier: float = 1.20
    backtest_regime_bear_risk_multiplier: float = 0.80
    backtest_dedup_same_day_event: bool = True
    backtest_use_event_quality_filter: bool = False
    backtest_event_quality_min_score: int = 70
    backtest_event_quality_fail_open: bool = True
    backtest_allow_unknown_with_llm: bool = True
    backtest_allow_next_session_entry: bool = True
    backtest_regular_session_only: bool = True
    backtest_intraday_flatten: bool = False
    backtest_max_next_session_delay_min: int = 1080
    backtest_conviction_position_sizing: bool = True
    backtest_conviction_min_risk_multiplier: float = 1.0
    backtest_conviction_max_risk_multiplier: float = 2.5
    backtest_conviction_position_floor: float = 0.60

    min_trade_confidence: int = 70

    # ── Signal Validation Layer ───────────────────────────────────────────────
    validation_enabled: bool = True
    # Minimum review_score to allow execution (0–100). Signals below this are REJECTED.
    validation_min_review_score: int = 40
    # Block execution when novelty == STALE
    validation_reject_on_stale: bool = True
    # Block execution when novelty == DUPLICATE
    validation_reject_on_duplicate: bool = True
    # Price-move threshold: if ticker moved > this % since event_time, flag priced-in risk HIGH
    validation_price_move_threshold_pct: float = 3.0
    # When recommendation is DOWNWEIGHT, still allow execution (just flag it)
    validation_allow_downweight_execution: bool = True
    # Staleness: news older than this many minutes is flagged STALE
    validation_stale_minutes: int = 120
    validation_corroboration_window_minutes: int = 180
    # Mark news as historical backfill in UI/API when ingestion lag exceeds this threshold.
    news_backfill_delay_minutes: int = 180

    earnings_calendar_auto_refresh: bool = True
    earnings_calendar_refresh_interval_hours: int = 24
    earnings_calendar_lookback_days: int = 30
    earnings_calendar_lookahead_days: int = 90
    earnings_release_ingestion_lookback_days: int = 7
    macro_context_lookback_days: int = 30

    sec_poller_limit: int = 100
    rss_sources: list[str] = Field(
        default_factory=lambda: [
            # CNBC — multiple topic feeds (Tier 1, verified working)
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # General
            "https://www.cnbc.com/id/15839069/device/rss/rss.html",   # Markets
            "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # Economy
            "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # Tech
            # Other Tier-1 sources (verified working 2026-03)
            "https://www.marketwatch.com/rss/topstories",
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
            "https://www.ft.com/markets?format=rss",
            # Tier-2 (aggregated, reliable)
            "https://finance.yahoo.com/rss/topstories",
            # Geopolitical / world events — feed the MacroAnalystAgent
            "https://feeds.bbci.co.uk/news/world/rss.xml",            # BBC World
            "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",  # BBC US policy
            "https://feeds.bbci.co.uk/news/business/rss.xml",         # BBC Business (economic impact)
            "https://www.aljazeera.com/xml/rss/all.xml",               # Al Jazeera
            "https://api.axios.com/feed/",                             # Axios (full articles)
        ]
    )
    # Enable per-ticker Yahoo Finance RSS headlines during ingestion
    enable_ticker_rss: bool = True
    # Max number of tickers to fetch per-ticker Yahoo Finance RSS for (0 = all sp100)
    ticker_rss_max_tickers: int = 0

    sp100_tickers: list[str] = Field(default_factory=lambda: SP100_TICKERS.copy())

    # ── Agent System ──────────────────────────────────────────────────────────
    agent_mode_enabled: bool = True
    # Optionally override which tickers the agent graph runs on (empty = all SP100)
    agent_tickers_override: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # How many minutes of Bar1m history to load for technical analysis
    agent_technicals_lookback_bars: int = 390  # ~1 trading day of 1m bars

    # ── FRED Macroeconomic Data ───────────────────────────────────────────────
    fred_api_key: str = ""
    # FRED series to fetch; defaults cover the most market-relevant indicators
    fred_series: list[str] = Field(
        default_factory=lambda: [
            "CPIAUCSL",   # CPI (inflation)
            "GDP",        # Gross Domestic Product
            "UNRATE",     # Unemployment rate
            "FEDFUNDS",   # Federal Funds Rate
            "DGS10",      # 10-Year Treasury Yield
            "UMCSENT",    # University of Michigan Consumer Sentiment
            "T10Y2Y",     # 10Y-2Y yield spread (recession indicator)
            "VIXCLS",     # VIX (market fear index via FRED)
        ]
    )
    fred_refresh_interval_hours: int = 24

    # ── Fundamentals & Analyst Data ───────────────────────────────────────────
    fundamentals_refresh_interval_days: int = 7
    analyst_ratings_refresh_interval_days: int = 1

    # ── Broker (Alpaca) ───────────────────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"  # paper trading endpoint

    # ── Live Trading ──────────────────────────────────────────────────────────
    live_trading_enabled: bool = False
    # Tickers to trade live; falls back to agent_tickers_override if empty
    live_trading_tickers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: DEFAULT_LIVE_TICKERS.copy()
    )
    # Optional source whitelist for live news analysis/triggering.
    # Empty list means "all sources".
    live_allowed_sources: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: DEFAULT_LIVE_ALLOWED_SOURCES.copy()
    )
    # How often (seconds) the live cycle runs during market hours (default 5 min)
    live_cycle_interval_seconds: int = 300
    # Effective scheduled interval by market session.
    # Open session cadence (default: 15 minutes)
    live_open_cycle_seconds: int = 900
    # Closed / pre-market / after-hours cadence (default: 120 minutes)
    live_closed_cycle_seconds: int = 7200
    # Maximum portfolio allocation per position (0.0–1.0)
    live_max_position_pct: float = 0.10
    # Live execution confidence gate; BUY/SHORT/SELL below this are downgraded to HOLD.
    live_min_confidence: int = 50
    # Allow order placement during pre-market session (default: False)
    live_allow_premarket: bool = False
    # Default action when disabling live trading from control plane.
    live_disable_default_mode: str = "CANCEL_ORDERS"
    # Refuse new live orders when local Bar1m cache is older than this threshold.
    live_data_max_age_minutes: float = 20.0
    # Watchdog: reap stuck live cycles so future scheduled cycles can continue.
    live_cycle_stale_seconds: int = 900
    # Watchdog: reap stuck bar refresh runs and avoid duplicate refresh overlap.
    bar_backfill_stale_seconds: int = 900
    # Event-driven live mode: run full graph when new tradeable news arrives.
    live_event_driven_mode: bool = True
    # When live is enabled, warm up ingestion/cache first before placing orders.
    live_enable_warmup_minutes: int = 15
    # Legacy fallback interval (kept for backward compatibility).
    live_fallback_cycle_seconds: int = 600
    # Per-ticker debounce when there is no new tradeable event for that ticker.
    live_ticker_cooldown_minutes: int = 60
    # Startup/ramp guardrails right after enabling live.
    live_startup_max_new_positions: int = 2
    live_startup_ramp_minutes: int = 30
    # Portfolio concentration caps during live execution.
    live_max_net_long_exposure_pct: float = 0.35
    live_max_net_short_exposure_pct: float = 0.35
    live_max_same_direction_positions: int = 4
    live_max_same_theme_direction_positions: int = 2
    # Fast-path cache TTL for macro/fundamentals agent outputs.
    live_fast_path_macro_ttl_min: int = 60
    live_fast_path_fund_ttl_min: int = 120
    # Portfolio manager LLM guardrails for live cycles.
    live_portfolio_llm_timeout_seconds: float = 20.0
    live_portfolio_llm_max_retries: int = 2
    # Pull Finnhub per-ticker company-news during live cycles (more relevant than general feed).
    enable_finnhub_company_news_live: bool = True
    finnhub_company_news_live_lookback_days: int = 2
    # Close-window overnight guard; runs independently from live enable/disable.
    live_overnight_risk_enabled: bool = True
    # When enabled, force full flatten before close instead of reduce/alert.
    live_flatten_before_close: bool = False
    live_overnight_mode: str = "REDUCE"
    live_overnight_max_gross_exposure_pct: float = 0.25
    live_overnight_rebalance_minutes_before_close: int = 5
    live_overnight_run_when_disabled: bool = True
    # Capital confirmation layer (volume/follow-through/relative-strength).
    flow_confirmation_enabled: bool = True
    flow_confirmation_soft_gate: bool = True
    # Base agent weights (direction bias).
    agent_weight_news: float = 0.60
    agent_weight_technicals: float = 0.20
    agent_weight_macro: float = 0.10
    agent_weight_fundamentals: float = 0.10
    # Entry-planning controls (Live First v1).
    live_entry_planning_enabled: bool = True
    live_entry_plan_default_valid_minutes: int = 180
    live_entry_plan_breakout_lookback_min: int = 15
    live_entry_plan_default_pullback_pct: float = 0.5

    @field_validator("agent_tickers_override", "live_trading_tickers", mode="before")
    @classmethod
    def _parse_csv_ticker_list(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).upper().strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(item).upper().strip() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [part.upper().strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("live_allowed_sources", mode="before")
    @classmethod
    def _parse_csv_source_list(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(item).strip().lower() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [part.strip().lower() for part in stripped.split(",") if part.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
