from __future__ import annotations

import logging
import re
from time import sleep
from typing import Iterable

import feedparser
import httpx
from dateutil import parser as dt_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.utils import make_hash, utc_now
from app.db.models import IngestionCursor
from app.ingestion.types import SourceCheck
from app.schemas.types import RawNewsItem

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ATOM_FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={query}&owner=exclude&count=60&output=atom"
SUPPORTED_FORMS = {"8-K", "10-Q", "10-K", "6-K", "13D", "13G"}
RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}

logger = logging.getLogger(__name__)


class SecClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {
            "User-Agent": settings.sec_user_agent,
            "Accept": "application/json",
        }

    def _get_cursor(self, session: Session, key: str, default: str) -> str:
        row = session.execute(select(IngestionCursor).where(IngestionCursor.cursor_key == key)).scalar_one_or_none()
        return row.cursor_value if row else default

    def _set_cursor(self, session: Session, key: str, value: str) -> None:
        row = session.execute(select(IngestionCursor).where(IngestionCursor.cursor_key == key)).scalar_one_or_none()
        if row:
            row.cursor_value = value
            return
        session.add(IngestionCursor(cursor_key=key, cursor_value=value))

    def _ticker_cik_map(self) -> dict[str, str]:
        with httpx.Client(timeout=15.0, headers=self.headers) as client:
            resp = client.get(SEC_TICKERS_URL)
            resp.raise_for_status()
            payload = resp.json()

        mapping: dict[str, str] = {}
        for row in payload.values():
            ticker = str(row.get("ticker") or "").upper().strip()
            cik_digits = "".join(ch for ch in str(row.get("cik_str", "")) if ch.isdigit())
            cik = cik_digits.zfill(10) if cik_digits else ""
            if ticker and cik:
                mapping[ticker] = cik
        return mapping

    def _round_robin_tickers(self, session: Session, universe: list[str], batch_size: int = 8) -> Iterable[str]:
        if not universe:
            return []
        offset = int(self._get_cursor(session, "sec_offset", "0"))
        n = len(universe)
        batch = [universe[(offset + i) % n] for i in range(min(batch_size, n))]
        self._set_cursor(session, "sec_offset", str((offset + len(batch)) % n))
        return batch

    def _request_json_with_retry(
        self, client: httpx.Client, url: str, retries: int = 3
    ) -> tuple[dict | None, int | None, str | None]:
        last_error: str | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json(), 200, None

                snippet = resp.text[:200].replace("\n", " ")
                err = f"HTTP {resp.status_code}: {snippet}"
                if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                    last_error = err
                    sleep(0.4 * attempt)
                    continue
                return None, resp.status_code, err
            except Exception as exc:
                last_error = str(exc)
                if attempt < retries:
                    sleep(0.4 * attempt)
                    continue
        return None, None, last_error or "request_failed"

    @staticmethod
    def _accession_from_text(text: str) -> str | None:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", text)
        return m.group(1) if m else None

    def _fetch_atom_fallback(
        self, client: httpx.Client, ticker: str, cik: str
    ) -> tuple[list[RawNewsItem], str | None]:
        url = SEC_ATOM_FEED_URL.format(query=ticker)
        try:
            resp = client.get(url, headers={"Accept": "application/atom+xml"})
            if resp.status_code != 200:
                return [], f"ATOM fallback HTTP {resp.status_code}"
            feed = feedparser.parse(resp.text)
        except Exception as exc:
            return [], f"ATOM fallback failed: {exc}"

        if not feed.entries:
            return [], "ATOM fallback has no entries"

        out: list[RawNewsItem] = []
        for entry in feed.entries:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            form = str(entry.get("category") or "").strip().upper()
            if not form and title:
                form = title.split()[0].upper()
            if form not in SUPPORTED_FORMS:
                continue
            if not link:
                continue

            updated_raw = entry.get("updated") or entry.get("published") or utc_now().isoformat()
            try:
                published = dt_parser.parse(str(updated_raw))
            except Exception:
                published = utc_now()

            summary = str(entry.get("summary") or "").strip()
            accession = (
                self._accession_from_text(str(entry.get("id") or ""))
                or self._accession_from_text(summary)
                or self._accession_from_text(link)
                or f"{ticker}-{published.date().isoformat()}-{form}"
            )
            item_hash = make_hash("sec", accession, ticker)
            out.append(
                RawNewsItem(
                    source="sec",
                    url=link,
                    title=f"{ticker} filed {form}",
                    body=summary or title,
                    published_at=published,
                    ingested_at=utc_now(),
                    hash=item_hash,
                    source_tier=0,
                    metadata={
                        "ticker": ticker,
                        "cik": cik,
                        "form": form,
                        "accession": accession,
                        "fallback": "atom",
                    },
                )
            )

        if not out:
            return [], "ATOM fallback entries parsed but none matched supported forms"
        return out, None

    def fetch(self, session: Session) -> tuple[list[RawNewsItem], SourceCheck]:
        if not self.settings.enable_sec:
            return [], SourceCheck(
                source_key="sec",
                source_name="sec",
                source_type="sec",
                display_name="SEC EDGAR",
                status="OFFLINE",
                error_message="SEC source disabled by config",
            )

        try:
            mapping = self._ticker_cik_map()
        except Exception as exc:
            logger.warning("SEC ticker map fetch failed: %s", exc)
            return [], SourceCheck(
                source_key="sec",
                source_name="sec",
                source_type="sec",
                display_name="SEC EDGAR",
                status="OFFLINE",
                error_message=f"Ticker map request failed: {exc}",
            )

        tickers = [t for t in self.settings.sp100_tickers if t in mapping]
        selected = list(self._round_robin_tickers(session, tickers, batch_size=10))
        if not selected:
            return [], SourceCheck(
                source_key="sec",
                source_name="sec",
                source_type="sec",
                display_name="SEC EDGAR",
                status="ONLINE",
                details={"selected_tickers": 0, "items": 0},
            )

        items: list[RawNewsItem] = []
        failed_requests: list[str] = []
        success_requests = 0
        atom_fallback_tickers = 0

        with httpx.Client(timeout=15.0, headers=self.headers) as client:
            for ticker in selected:
                cik = mapping[ticker]
                url = SEC_SUBMISSIONS_URL.format(cik=cik)
                data, status_code, err = self._request_json_with_retry(client, url)

                if data is None:
                    atom_items: list[RawNewsItem] = []
                    atom_error: str | None = None
                    if status_code == 404:
                        atom_items, atom_error = self._fetch_atom_fallback(client, ticker=ticker, cik=cik)
                        if atom_items:
                            atom_fallback_tickers += 1
                            success_requests += 1
                            items.extend(atom_items)
                            logger.warning(
                                "SEC submissions 404 for %s, used ATOM fallback and got %s items",
                                ticker,
                                len(atom_items),
                            )
                            continue
                    msg = f"{ticker}: {err or 'unknown_error'}"
                    if atom_error:
                        msg = f"{msg}; atom_fallback={atom_error}"
                    failed_requests.append(msg)
                    logger.warning("SEC submissions failed for %s: %s", ticker, msg)
                    continue

                success_requests += 1
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                filing_dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                docs = recent.get("primaryDocument", [])

                rows = zip(forms, filing_dates, accessions, docs, strict=False)
                for form, filing_date, accession, doc in rows:
                    if form not in SUPPORTED_FORMS:
                        continue
                    if not accession:
                        continue
                    accession_plain = accession.replace("-", "")
                    archive_cik = cik.lstrip("0") or "0"
                    doc_name = doc or f"{accession}-index.html"
                    filing_url = f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_plain}/{doc_name}"
                    published = dt_parser.parse(filing_date) if filing_date else utc_now()
                    title = f"{ticker} filed {form}"
                    body = f"SEC filing {form} accession {accession}"
                    item_hash = make_hash("sec", accession, ticker)
                    items.append(
                        RawNewsItem(
                            source="sec",
                            url=filing_url,
                            title=title,
                            body=body,
                            published_at=published,
                            ingested_at=utc_now(),
                            hash=item_hash,
                            source_tier=0,
                            metadata={
                                "ticker": ticker,
                                "cik": cik,
                                "form": form,
                                "accession": accession,
                            },
                        )
                    )

        unique: dict[str, RawNewsItem] = {}
        for item in items:
            unique[item.hash] = item
        out_items = list(unique.values())

        if success_requests == 0 and failed_requests:
            return out_items, SourceCheck(
                source_key="sec",
                source_name="sec",
                source_type="sec",
                display_name="SEC EDGAR",
                status="OFFLINE",
                error_message="; ".join(failed_requests[:3]),
                details={
                    "selected_tickers": len(selected),
                    "failed_requests": len(failed_requests),
                    "items": len(out_items),
                    "atom_fallback_tickers": atom_fallback_tickers,
                },
            )

        return out_items, SourceCheck(
            source_key="sec",
            source_name="sec",
            source_type="sec",
            display_name="SEC EDGAR",
            status="ONLINE",
            details={
                "selected_tickers": len(selected),
                "success_requests": success_requests,
                "failed_requests": len(failed_requests),
                "items": len(out_items),
                "atom_fallback_tickers": atom_fallback_tickers,
            },
        )
