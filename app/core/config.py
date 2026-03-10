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
    enable_term_management: bool = True
    term_short_horizon_min: int = 60
    term_mid_horizon_min: int = 240
    term_long_horizon_min: int = 1440

    initial_nav: float = 100_000.0
    max_position_pct: float = 0.10
    max_gross_exposure_pct: float = 1.00
    daily_loss_limit_pct: float = -0.03
    default_slippage_bps: float = 4.0
    short_borrow_apr: float = 0.03
    default_horizon_min: int = 120
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    backtest_hard_stops: bool = True
    backtest_risk_sizing: bool = True
    backtest_risk_per_trade_pct: float = 0.001
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
