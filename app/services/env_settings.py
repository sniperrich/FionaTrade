from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
from typing import Any

from app.core.config import Settings

_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EnvSettingsService:
    """Read/write selected runtime configuration into project .env."""

    BOOL_KEYS = {
        "ENABLE_SEC",
        "ENABLE_RSS",
        "ENABLE_FINNHUB",
        "ENABLE_FINNHUB_COMPANY_NEWS_LIVE",
        "ENABLE_EARNINGS_RELEASE_SOURCE",
        "LIVE_TRADING_ENABLED",
        "LIVE_ALLOW_PREMARKET",
        "LIVE_EVENT_DRIVEN_MODE",
        "LIVE_OVERNIGHT_RISK_ENABLED",
        "LIVE_FLATTEN_BEFORE_CLOSE",
        "LIVE_OVERNIGHT_RUN_WHEN_DISABLED",
        "FLOW_CONFIRMATION_ENABLED",
        "FLOW_CONFIRMATION_SOFT_GATE",
    }
    INT_KEYS = {
        "POLL_INTERVAL_SECONDS",
        "LIVE_CYCLE_INTERVAL_SECONDS",
        "LIVE_OPEN_CYCLE_SECONDS",
        "LIVE_CLOSED_CYCLE_SECONDS",
        "LIVE_FALLBACK_CYCLE_SECONDS",
        "LIVE_TICKER_COOLDOWN_MINUTES",
        "LIVE_FAST_PATH_MACRO_TTL_MIN",
        "LIVE_FAST_PATH_FUND_TTL_MIN",
        "LIVE_MIN_CONFIDENCE",
        "MIN_TRADE_CONFIDENCE",
        "LIVE_PORTFOLIO_LLM_MAX_RETRIES",
        "FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS",
        "LIVE_OVERNIGHT_REBALANCE_MINUTES_BEFORE_CLOSE",
    }
    FLOAT_KEYS = {
        "LIVE_MAX_POSITION_PCT",
        "LIVE_DATA_MAX_AGE_MINUTES",
        "LIVE_PORTFOLIO_LLM_TIMEOUT_SECONDS",
        "LIVE_OVERNIGHT_MAX_GROSS_EXPOSURE_PCT",
        "AGENT_WEIGHT_NEWS",
        "AGENT_WEIGHT_TECHNICALS",
        "AGENT_WEIGHT_MACRO",
        "AGENT_WEIGHT_FUNDAMENTALS",
    }
    CSV_UPPER_KEYS = {
        "LIVE_TRADING_TICKERS",
        "AGENT_TICKERS_OVERRIDE",
    }
    CSV_LOWER_KEYS = {
        "LIVE_ALLOWED_SOURCES",
    }
    UPPER_VALUE_KEYS = {
        "LIVE_DISABLE_DEFAULT_MODE",
        "LIVE_OVERNIGHT_MODE",
    }
    KEY_ORDER = [
        "LIVE_TRADING_TICKERS",
        "AGENT_TICKERS_OVERRIDE",
        "LIVE_ALLOWED_SOURCES",
        "POLL_INTERVAL_SECONDS",
        "LIVE_CYCLE_INTERVAL_SECONDS",
        "LIVE_OPEN_CYCLE_SECONDS",
        "LIVE_CLOSED_CYCLE_SECONDS",
        "LIVE_MIN_CONFIDENCE",
        "MIN_TRADE_CONFIDENCE",
        "LIVE_MAX_POSITION_PCT",
        "LIVE_DISABLE_DEFAULT_MODE",
        "LIVE_DATA_MAX_AGE_MINUTES",
        "LIVE_EVENT_DRIVEN_MODE",
        "LIVE_FALLBACK_CYCLE_SECONDS",
        "LIVE_TICKER_COOLDOWN_MINUTES",
        "LIVE_FAST_PATH_MACRO_TTL_MIN",
        "LIVE_FAST_PATH_FUND_TTL_MIN",
        "LIVE_PORTFOLIO_LLM_TIMEOUT_SECONDS",
        "LIVE_PORTFOLIO_LLM_MAX_RETRIES",
        "LIVE_OVERNIGHT_RISK_ENABLED",
        "LIVE_FLATTEN_BEFORE_CLOSE",
        "LIVE_OVERNIGHT_MODE",
        "LIVE_OVERNIGHT_MAX_GROSS_EXPOSURE_PCT",
        "LIVE_OVERNIGHT_REBALANCE_MINUTES_BEFORE_CLOSE",
        "LIVE_OVERNIGHT_RUN_WHEN_DISABLED",
        "ENABLE_FINNHUB_COMPANY_NEWS_LIVE",
        "FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS",
        "FLOW_CONFIRMATION_ENABLED",
        "FLOW_CONFIRMATION_SOFT_GATE",
        "AGENT_WEIGHT_NEWS",
        "AGENT_WEIGHT_TECHNICALS",
        "AGENT_WEIGHT_MACRO",
        "AGENT_WEIGHT_FUNDAMENTALS",
        "LIVE_TRADING_ENABLED",
        "LIVE_ALLOW_PREMARKET",
        "ENABLE_SEC",
        "ENABLE_RSS",
        "ENABLE_FINNHUB",
        "ENABLE_EARNINGS_RELEASE_SOURCE",
        "LLM_MODEL",
        "LLM_BASE_URL",
    ]

    def __init__(self, env_path: Path | None = None):
        self.env_path = env_path or (Path(__file__).resolve().parents[2] / ".env")

    def snapshot(self, settings: Settings) -> dict[str, Any]:
        return {
            "env_file": str(self.env_path),
            "editable": {
                "LIVE_TRADING_TICKERS": ",".join(settings.live_trading_tickers or []),
                "AGENT_TICKERS_OVERRIDE": ",".join(settings.agent_tickers_override or []),
                "LIVE_ALLOWED_SOURCES": ",".join(settings.live_allowed_sources or []),
                "POLL_INTERVAL_SECONDS": settings.poll_interval_seconds,
                "LIVE_CYCLE_INTERVAL_SECONDS": settings.live_cycle_interval_seconds,
                "LIVE_OPEN_CYCLE_SECONDS": settings.live_open_cycle_seconds,
                "LIVE_CLOSED_CYCLE_SECONDS": settings.live_closed_cycle_seconds,
                "LIVE_MIN_CONFIDENCE": settings.live_min_confidence,
                "MIN_TRADE_CONFIDENCE": settings.min_trade_confidence,
                "LIVE_MAX_POSITION_PCT": settings.live_max_position_pct,
                "LIVE_DISABLE_DEFAULT_MODE": settings.live_disable_default_mode,
                "LIVE_DATA_MAX_AGE_MINUTES": settings.live_data_max_age_minutes,
                "LIVE_EVENT_DRIVEN_MODE": settings.live_event_driven_mode,
                "LIVE_FALLBACK_CYCLE_SECONDS": settings.live_fallback_cycle_seconds,
                "LIVE_TICKER_COOLDOWN_MINUTES": settings.live_ticker_cooldown_minutes,
                "LIVE_FAST_PATH_MACRO_TTL_MIN": settings.live_fast_path_macro_ttl_min,
                "LIVE_FAST_PATH_FUND_TTL_MIN": settings.live_fast_path_fund_ttl_min,
                "LIVE_PORTFOLIO_LLM_TIMEOUT_SECONDS": settings.live_portfolio_llm_timeout_seconds,
                "LIVE_PORTFOLIO_LLM_MAX_RETRIES": settings.live_portfolio_llm_max_retries,
                "LIVE_OVERNIGHT_RISK_ENABLED": settings.live_overnight_risk_enabled,
                "LIVE_FLATTEN_BEFORE_CLOSE": settings.live_flatten_before_close,
                "LIVE_OVERNIGHT_MODE": settings.live_overnight_mode,
                "LIVE_OVERNIGHT_MAX_GROSS_EXPOSURE_PCT": settings.live_overnight_max_gross_exposure_pct,
                "LIVE_OVERNIGHT_REBALANCE_MINUTES_BEFORE_CLOSE": settings.live_overnight_rebalance_minutes_before_close,
                "LIVE_OVERNIGHT_RUN_WHEN_DISABLED": settings.live_overnight_run_when_disabled,
                "ENABLE_FINNHUB_COMPANY_NEWS_LIVE": settings.enable_finnhub_company_news_live,
                "FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS": settings.finnhub_company_news_live_lookback_days,
                "FLOW_CONFIRMATION_ENABLED": settings.flow_confirmation_enabled,
                "FLOW_CONFIRMATION_SOFT_GATE": settings.flow_confirmation_soft_gate,
                "AGENT_WEIGHT_NEWS": settings.agent_weight_news,
                "AGENT_WEIGHT_TECHNICALS": settings.agent_weight_technicals,
                "AGENT_WEIGHT_MACRO": settings.agent_weight_macro,
                "AGENT_WEIGHT_FUNDAMENTALS": settings.agent_weight_fundamentals,
                "LIVE_TRADING_ENABLED": settings.live_trading_enabled,
                "LIVE_ALLOW_PREMARKET": settings.live_allow_premarket,
                "ENABLE_SEC": settings.enable_sec,
                "ENABLE_RSS": settings.enable_rss,
                "ENABLE_FINNHUB": settings.enable_finnhub,
                "ENABLE_EARNINGS_RELEASE_SOURCE": settings.enable_earnings_release_source,
                "LLM_MODEL": settings.llm_model,
                "LLM_BASE_URL": settings.llm_base_url,
            },
            "key_order": self.KEY_ORDER,
        }

    @staticmethod
    def _line_key(line: str) -> str | None:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            return None
        if raw.startswith("export "):
            raw = raw[len("export "):].strip()
        if "=" not in raw:
            return None
        key = raw.split("=", 1)[0].strip()
        if not _ENV_KEY_RE.match(key):
            return None
        return key

    @staticmethod
    def _normalize_csv_upper(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            parts = value.split(",")
        elif isinstance(value, Iterable):
            parts = [str(item) for item in value]
        else:
            parts = [str(value)]
        cleaned: list[str] = []
        seen: set[str] = set()
        for part in parts:
            token = str(part).strip().upper()
            if not token or token in seen:
                continue
            cleaned.append(token)
            seen.add(token)
        return ",".join(cleaned)

    @staticmethod
    def _normalize_csv_lower(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            parts = value.split(",")
        elif isinstance(value, Iterable):
            parts = [str(item) for item in value]
        else:
            parts = [str(value)]
        cleaned: list[str] = []
        seen: set[str] = set()
        for part in parts:
            token = str(part).strip().lower()
            if not token or token in seen:
                continue
            cleaned.append(token)
            seen.add(token)
        return ",".join(cleaned)

    def _normalize_value(self, key: str, value: Any) -> str:
        if key in self.CSV_UPPER_KEYS:
            return self._normalize_csv_upper(value)
        if key in self.CSV_LOWER_KEYS:
            return self._normalize_csv_lower(value)
        if key in self.UPPER_VALUE_KEYS:
            return str(value or "").strip().upper()
        if key in self.BOOL_KEYS:
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "y", "on"}:
                    return "true"
                if normalized in {"0", "false", "no", "n", "off"}:
                    return "false"
                raise ValueError(f"{key} expects boolean value")
            return "true" if bool(value) else "false"
        if key in self.INT_KEYS:
            try:
                return str(int(value))
            except Exception as exc:
                raise ValueError(f"{key} expects integer value") from exc
        if key in self.FLOAT_KEYS:
            try:
                return str(float(value))
            except Exception as exc:
                raise ValueError(f"{key} expects numeric value") from exc
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _validate_env_key(key: str) -> str:
        normalized = str(key or "").strip().upper()
        if not _ENV_KEY_RE.match(normalized):
            raise ValueError(f"invalid env key: {key!r}")
        return normalized

    def _parse_extra_updates(self, text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"extra_updates line {lineno} must be KEY=VALUE")
            key, value = line.split("=", 1)
            env_key = self._validate_env_key(key)
            out[env_key] = value.strip()
        return out

    def apply_updates(self, updates: dict[str, Any], extra_updates_text: str = "") -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")

        normalized_updates: dict[str, str] = {}
        for raw_key, raw_value in updates.items():
            env_key = self._validate_env_key(str(raw_key))
            normalized_updates[env_key] = self._normalize_value(env_key, raw_value)

        if extra_updates_text:
            normalized_updates.update(self._parse_extra_updates(extra_updates_text))

        if not normalized_updates:
            return {
                "env_file": str(self.env_path),
                "written_keys": [],
                "created": not self.env_path.exists(),
            }

        env_existed = self.env_path.exists()
        existing_lines = self.env_path.read_text(encoding="utf-8").splitlines() if env_existed else []
        key_to_index: dict[str, int] = {}
        for idx, line in enumerate(existing_lines):
            key = self._line_key(line)
            if key and key not in key_to_index:
                key_to_index[key] = idx

        for key, value in normalized_updates.items():
            new_line = f"{key}={value}"
            if key in key_to_index:
                existing_lines[key_to_index[key]] = new_line
            else:
                existing_lines.append(new_line)

        rendered = "\n".join(existing_lines).strip("\n") + "\n"
        self.env_path.write_text(rendered, encoding="utf-8")
        return {
            "env_file": str(self.env_path),
            "written_keys": list(normalized_updates.keys()),
            "created": not env_existed,
        }
