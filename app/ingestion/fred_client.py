from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_app_logger
from app.db.models import MacroIndicator

logger = get_app_logger()

_BASE_URL = "https://api.stlouisfed.org/fred"

SERIES_NAMES: dict[str, str] = {
    "CPIAUCSL": "CPI (Consumer Price Index)",
    "GDP": "Gross Domestic Product",
    "UNRATE": "Unemployment Rate",
    "FEDFUNDS": "Federal Funds Rate",
    "DGS10": "10-Year Treasury Yield",
    "UMCSENT": "Consumer Sentiment (Michigan)",
    "T10Y2Y": "10Y-2Y Treasury Spread",
    "VIXCLS": "VIX Volatility Index",
}


class FREDClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # Private helpers (accept a shared httpx.Client for connection reuse)
    # ------------------------------------------------------------------

    def _fetch_observations(
        self,
        client: httpx.Client,
        series_id: str,
        observation_start: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, str | int] = {
            "series_id": series_id,
            "api_key": self.settings.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        if observation_start:
            params["observation_start"] = observation_start

        try:
            resp = client.get(f"{_BASE_URL}/series/observations", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("FRED fetch_observations %s failed: %s", series_id, exc)
            return []

        result: list[dict] = []
        for obs in resp.json().get("observations", []):
            raw_value = obs.get("value", ".")
            result.append({
                "date": obs["date"],
                "value": None if raw_value == "." else raw_value,
            })
        return result

    def _fetch_info(self, client: httpx.Client, series_id: str) -> dict:
        params = {
            "series_id": series_id,
            "api_key": self.settings.fred_api_key,
            "file_type": "json",
        }
        try:
            resp = client.get(f"{_BASE_URL}/series", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("FRED get_series_info %s failed: %s", series_id, exc)
            return {}

        seriess = resp.json().get("seriess", [])
        if not seriess:
            return {}
        info = seriess[0]
        return {
            "title": info.get("title", ""),
            "units": info.get("units_short", info.get("units", "")),
            "frequency": info.get("frequency_short", info.get("frequency", "")),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_series(
        self,
        series_id: str,
        observation_start: str | None = None,
    ) -> list[dict]:
        """Return up to 50 most-recent observations for *series_id*.

        Each element is ``{"date": "YYYY-MM-DD", "value": "3.5" | None}``.
        FRED's sentinel missing-data value ``"."`` is normalised to ``None``.
        """
        with httpx.Client(timeout=15.0) as client:
            return self._fetch_observations(client, series_id, observation_start, limit=50)

    def get_series_info(self, series_id: str) -> dict:
        """Return ``{"title": ..., "units": ..., "frequency": ...}`` for *series_id*."""
        with httpx.Client(timeout=15.0) as client:
            return self._fetch_info(client, series_id)

    def upsert_indicators(
        self,
        session: Session,
        series_ids: list[str] | None = None,
    ) -> dict:
        """Fetch and upsert macro observations into ``macro_indicators``.

        Args:
            session: Active SQLAlchemy session (caller owns commit/rollback).
            series_ids: Series to process; defaults to ``settings.fred_series``.

        Returns:
            ``{"fetched": N, "upserted": N, "series": [...]}`` on success, or
            ``{"skipped": True}`` when the API key is not configured.
        """
        if not self.settings.fred_api_key:
            logger.warning("FRED API key is not configured; skipping macro indicator fetch")
            return {"skipped": True}

        target_series: list[str] = series_ids if series_ids is not None else list(self.settings.fred_series)
        total_fetched = 0
        total_upserted = 0
        processed: list[str] = []
        now = datetime.now(timezone.utc)

        with httpx.Client(timeout=15.0) as client:
            for series_id in target_series:
                logger.debug("FRED fetching series %s", series_id)
                try:
                    observations = self._fetch_observations(client, series_id, limit=60)
                    if not observations:
                        continue

                    info = self._fetch_info(client, series_id)
                    indicator_name = SERIES_NAMES.get(series_id, info.get("title", series_id))
                    unit = info.get("units", "")
                    total_fetched += len(observations)

                    for obs in observations:
                        obs_date = datetime.strptime(obs["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)

                        float_value: float | None = None
                        if obs["value"] is not None:
                            try:
                                float_value = float(obs["value"])
                            except (ValueError, TypeError):
                                float_value = None

                        stmt = (
                            insert(MacroIndicator)
                            .prefix_with("OR REPLACE")
                            .values(
                                series_id=series_id,
                                indicator_name=indicator_name,
                                observation_date=obs_date,
                                value=float_value,
                                unit=unit,
                                fetched_at=now,
                            )
                        )
                        session.execute(stmt)
                        total_upserted += 1

                    processed.append(series_id)

                except Exception as exc:
                    logger.error("FRED error processing series %s: %s", series_id, exc)
                    continue

        session.flush()
        return {"fetched": total_fetched, "upserted": total_upserted, "series": processed}
