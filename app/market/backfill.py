from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from time import sleep

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.config import Settings
from app.core.utils import ensure_utc
from app.db.models import Bar1m, Event


@dataclass
class MarketBackfillResult:
    start_date: str
    end_date: str
    tickers_requested: int
    tickers_processed: int
    chunk_days: int
    requests_ok: int
    requests_failed: int
    bars_inserted: int
    bars_skipped_existing: int
    alpaca_fallback_tickers: int
    alpaca_bars_inserted: int
    stooq_fallback_tickers: int
    stooq_bars_inserted: int
    errors: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class MarketBackfillService:
    BASE_URL = "https://finnhub.io/api/v1/stock/candle"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _parse_date_start(self, raw: str) -> datetime:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def _parse_date_end_exclusive(self, raw: str) -> datetime:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.replace(second=0, microsecond=0)

    def _build_ticker_universe(
        self,
        session: Session,
        start_dt: datetime,
        end_dt: datetime,
        requested_tickers: list[str] | None,
    ) -> list[str]:
        if requested_tickers:
            out = []
            seen = set()
            for ticker in requested_tickers:
                t = ticker.upper().strip()
                if not t or t in seen:
                    continue
                out.append(t)
                seen.add(t)
            return out

        events = session.execute(
            select(Event).where(and_(Event.event_time >= start_dt, Event.event_time < end_dt)).order_by(Event.id.asc())
        ).scalars().all()

        seen = set()
        tickers = []
        for event in events:
            for ticker in event.tickers or []:
                t = str(ticker).upper().strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                tickers.append(t)

        if tickers:
            return tickers

        return self.settings.sp100_tickers.copy()

    def _iter_chunks(self, start_dt: datetime, end_dt: datetime, chunk_days: int):
        cursor = start_dt
        step = timedelta(days=max(1, chunk_days))
        while cursor < end_dt:
            nxt = min(cursor + step, end_dt)
            yield cursor, nxt
            cursor = nxt

    def _fetch_chunk(self, ticker: str, start_dt: datetime, end_dt: datetime) -> tuple[list[dict], str | None]:
        params = {
            "symbol": ticker,
            "resolution": "1",
            "from": int(start_dt.timestamp()),
            "to": int(end_dt.timestamp()),
            "token": self.settings.finnhub_api_key,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(self.BASE_URL, params=params)
                if resp.status_code >= 400:
                    return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
        except Exception as exc:
            return [], str(exc)

        status = data.get("s")
        if status == "ok":
            ts = data.get("t", [])
            o = data.get("o", [])
            h = data.get("h", [])
            l = data.get("l", [])
            c = data.get("c", [])
            v = data.get("v", [])

            bars = []
            for i, t in enumerate(ts):
                bars.append(
                    {
                        "ts": datetime.fromtimestamp(t, tz=timezone.utc),
                        "open": float(o[i]),
                        "high": float(h[i]),
                        "low": float(l[i]),
                        "close": float(c[i]),
                        "volume": float(v[i]) if i < len(v) else 0.0,
                    }
                )
            return bars, None

        if status == "no_data":
            return [], None

        return [], f"Unexpected Finnhub payload status: {status}"

    def _fetch_stooq_daily(self, ticker: str, start_dt: datetime, end_dt: datetime) -> tuple[list[dict], str | None]:
        stooq_symbol = f"{ticker.lower().replace('.', '-')}.us"
        url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
        headers = {"User-Agent": "FionaTrade/0.1"}

        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.get(url)
                resp.raise_for_status()
                content = resp.text
        except Exception as exc:
            return [], f"stooq request failed: {exc}"

        reader = csv.DictReader(StringIO(content))
        bars: list[dict] = []
        for row in reader:
            date_raw = row.get("Date")
            open_raw = row.get("Open")
            high_raw = row.get("High")
            low_raw = row.get("Low")
            close_raw = row.get("Close")
            volume_raw = row.get("Volume")
            if not date_raw or not open_raw or open_raw == "N/D":
                continue

            try:
                day = datetime.fromisoformat(date_raw).replace(tzinfo=timezone.utc)
                if not (start_dt <= day < end_dt):
                    continue
                ts = datetime.combine(day.date(), time(hour=14, minute=30, tzinfo=timezone.utc))
                bars.append(
                    {
                        "ts": ts,
                        "open": float(open_raw),
                        "high": float(high_raw),
                        "low": float(low_raw),
                        "close": float(close_raw),
                        "volume": float(volume_raw) if volume_raw else 0.0,
                    }
                )
            except Exception:
                continue

        return bars, None

    def _fetch_yfinance_hourly(self, ticker: str, start_dt: datetime, end_dt: datetime) -> tuple[list[dict], str | None]:
        try:
            import yfinance as yf
        except ImportError:
            return [], "yfinance not installed"

        try:
            t = yf.Ticker(ticker)
            df = t.history(
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval="1h",
                auto_adjust=True,
            )
        except Exception as exc:
            return [], f"yfinance fetch failed: {exc}"

        if df is None or df.empty:
            return [], None

        bars: list[dict] = []
        for ts_idx, row in df.iterrows():
            try:
                ts = ts_idx.to_pydatetime().astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                if not (start_dt <= ts < end_dt):
                    continue
                bars.append({
                    "ts": ts,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume", 0) or 0),
                })
            except Exception:
                continue

        return bars, None

    def _fetch_alpaca_minute_bars(self, ticker: str, start_dt: datetime, end_dt: datetime) -> tuple[list[dict], str | None]:
        if not (self.settings.alpaca_api_key and self.settings.alpaca_api_secret):
            return [], "alpaca credentials not configured"

        try:
            from app.broker.alpaca import AlpacaBroker

            broker = AlpacaBroker(self.settings)
            bars = broker.get_bars(
                ticker,
                timeframe="1Min",
                start=start_dt.isoformat().replace("+00:00", "Z"),
                end=end_dt.isoformat().replace("+00:00", "Z"),
                limit=10000,
            )
        except Exception as exc:
            return [], f"alpaca request failed: {exc}"

        if not bars:
            return [], None

        parsed: list[dict] = []
        for bar in bars:
            try:
                ts = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                parsed.append(
                    {
                        "ts": ts,
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": float(bar.get("v", 0.0)),
                    }
                )
            except Exception:
                continue
        return parsed, None

    def run(
        self,
        session: Session,
        start_date: str,
        end_date: str,
        tickers: list[str] | None = None,
        chunk_days: int = 5,
        sleep_seconds: float = 0.12,
    ) -> MarketBackfillResult:
        if not self.settings.finnhub_api_key and not self.settings.market_backfill_allow_stooq_fallback:
            raise ValueError("FINNHUB_API_KEY is not configured")

        start_dt = self._parse_date_start(start_date)
        end_dt = self._parse_date_end_exclusive(end_date)
        if end_dt <= start_dt:
            raise ValueError("end_date must be greater than start_date")

        universe = self._build_ticker_universe(session, start_dt, end_dt, tickers)

        inserted = 0
        skipped_existing = 0
        req_ok = 0
        req_fail = 0
        alpaca_fallback_tickers = 0
        alpaca_bars_inserted = 0
        stooq_fallback_tickers = 0
        stooq_bars_inserted = 0
        errors: list[str] = []

        for ticker in universe:
            existing_ts = set(
                session.execute(
                    select(Bar1m.ts).where(and_(Bar1m.ticker == ticker, Bar1m.ts >= start_dt, Bar1m.ts < end_dt))
                ).scalars().all()
            )
            existing_ts = {ensure_utc(ts) for ts in existing_ts}

            ticker_forbidden = False  # 403 is per-ticker, not global
            finnhub_inserted_for_ticker = 0

            if self.settings.finnhub_api_key:
                for c_start, c_end in self._iter_chunks(start_dt, end_dt, chunk_days):
                    bars, err = self._fetch_chunk(ticker, c_start, c_end)
                    if err:
                        req_fail += 1
                        if "HTTP 403" in err:
                            ticker_forbidden = True
                            break  # 403 for this ticker only, stop its chunks
                        if len(errors) < 50:
                            errors.append(f"{ticker} {c_start.date()}~{c_end.date()}: {err}")
                        sleep(sleep_seconds)
                        continue

                    req_ok += 1
                    for bar in bars:
                        ts = ensure_utc(bar["ts"])
                        if ts in existing_ts:
                            skipped_existing += 1
                            continue
                        session.add(
                            Bar1m(
                                ticker=ticker,
                                ts=ts,
                                open=bar["open"],
                                high=bar["high"],
                                low=bar["low"],
                                close=bar["close"],
                                volume=bar["volume"],
                                source="finnhub_1m",
                            )
                        )
                        existing_ts.add(ts)
                        inserted += 1
                        finnhub_inserted_for_ticker += 1

                    sleep(sleep_seconds)

            if (
                self.settings.market_backfill_allow_stooq_fallback
                and finnhub_inserted_for_ticker == 0
            ):
                alpaca_bars, alpaca_err = self._fetch_alpaca_minute_bars(ticker, start_dt, end_dt)
                if alpaca_bars:
                    logger.info("alpaca 1m fallback for %s: %d bars", ticker, len(alpaca_bars))
                    alpaca_fallback_tickers += 1
                    for bar in alpaca_bars:
                        ts = ensure_utc(bar["ts"])
                        if ts in existing_ts:
                            skipped_existing += 1
                            continue
                        session.add(
                            Bar1m(
                                ticker=ticker,
                                ts=ts,
                                open=bar["open"],
                                high=bar["high"],
                                low=bar["low"],
                                close=bar["close"],
                                volume=bar["volume"],
                                source="alpaca_1m_fallback",
                            )
                        )
                        existing_ts.add(ts)
                        inserted += 1
                        alpaca_bars_inserted += 1
                else:
                    if alpaca_err and alpaca_err != "alpaca credentials not configured":
                        logger.warning("alpaca fallback failed for %s: %s", ticker, alpaca_err)
                    # Try yfinance hourly next (better resolution than stooq daily)
                    yf_bars, yf_err = self._fetch_yfinance_hourly(ticker, start_dt, end_dt)
                    if yf_bars:
                        logger.info("yfinance hourly fallback for %s: %d bars", ticker, len(yf_bars))
                        for bar in yf_bars:
                            ts = ensure_utc(bar["ts"])
                            if ts in existing_ts:
                                skipped_existing += 1
                                continue
                            session.add(
                                Bar1m(
                                    ticker=ticker,
                                    ts=ts,
                                    open=bar["open"],
                                    high=bar["high"],
                                    low=bar["low"],
                                    close=bar["close"],
                                    volume=bar["volume"],
                                    source="yfinance_hourly",
                                )
                            )
                            existing_ts.add(ts)
                            inserted += 1
                            stooq_fallback_tickers += 1
                    else:
                        if yf_err:
                            logger.warning("yfinance fallback failed for %s: %s", ticker, yf_err)
                        # Fall back to stooq daily
                        stooq_bars, stooq_err = self._fetch_stooq_daily(ticker, start_dt, end_dt)
                        if stooq_err:
                            if len(errors) < 50:
                                errors.append(f"{ticker} stooq fallback: {stooq_err}")
                        else:
                            if stooq_bars:
                                stooq_fallback_tickers += 1
                            for bar in stooq_bars:
                                ts = ensure_utc(bar["ts"])
                                if ts in existing_ts:
                                    skipped_existing += 1
                                    continue
                                session.add(
                                    Bar1m(
                                        ticker=ticker,
                                        ts=ts,
                                        open=bar["open"],
                                        high=bar["high"],
                                        low=bar["low"],
                                        close=bar["close"],
                                        volume=bar["volume"],
                                        source="stooq_daily_fallback",
                                    )
                                )
                                existing_ts.add(ts)
                                inserted += 1
                                stooq_bars_inserted += 1

            session.flush()

        return MarketBackfillResult(
            start_date=start_dt.isoformat(),
            end_date=end_dt.isoformat(),
            tickers_requested=len(universe),
            tickers_processed=len(universe),
            chunk_days=chunk_days,
            requests_ok=req_ok,
            requests_failed=req_fail,
            bars_inserted=inserted,
            bars_skipped_existing=skipped_existing,
            alpaca_fallback_tickers=alpaca_fallback_tickers,
            alpaca_bars_inserted=alpaca_bars_inserted,
            stooq_fallback_tickers=stooq_fallback_tickers,
            stooq_bars_inserted=stooq_bars_inserted,
            errors=errors,
        )
