from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.universe import SP100_TICKERS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "FionaTrade"
    env: str = "dev"
    database_url: str = "sqlite:///./fionatrade.db"
    sqlite_busy_timeout_seconds: float = 30.0

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

    sec_user_agent: str = "FionaTrade/0.1 (your-email@example.com)"
    finnhub_api_key: str = ""

    llm_provider: str = "openai-compatible"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_classifier_model: str = "gemini-3-flash"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3
    llm_retry_backoff_seconds: float = 1.5
    llm_retry_backoff_multiplier: float = 1.8
    llm_retry_max_delay_seconds: float = 12.0
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
    backtest_dedup_same_day_event: bool = False
    backtest_use_event_quality_filter: bool = False
    backtest_event_quality_min_score: int = 70
    backtest_event_quality_fail_open: bool = True
    backtest_allow_unknown_with_llm: bool = True
    backtest_allow_next_session_entry: bool = True
    backtest_regular_session_only: bool = True
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

    earnings_calendar_auto_refresh: bool = True
    earnings_calendar_refresh_interval_hours: int = 24
    earnings_calendar_lookback_days: int = 30
    earnings_calendar_lookahead_days: int = 90
    macro_context_lookback_days: int = 30

    sec_poller_limit: int = 100
    rss_sources: list[str] = Field(
        default_factory=lambda: [
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "https://www.marketwatch.com/rss/topstories",
        ]
    )

    sp100_tickers: list[str] = Field(default_factory=lambda: SP100_TICKERS.copy())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
