from __future__ import annotations

import json
import logging
import re
from time import sleep
from typing import Iterable
from urllib.parse import urljoin

import feedparser
import httpx
from dateutil import parser as dt_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency fallback
    OpenAI = None

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
FETCH_BODY_FORMS = {"8-K", "6-K"}
_ITEM_202_RE = re.compile(r"item\s*2\.02\b.*?results of operations and financial condition", re.IGNORECASE | re.DOTALL)
_EXHIBIT_991_ROW_RE = re.compile(r"<tr[^>]*>.*?(?:99\.1|ex[-\s]*99\.1).*?</tr>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_SEC_EARNINGS_SIGNAL_RE = re.compile(
    r"\b(quarterly results|financial results|earnings release|reported .*? quarter|"
    r"results of operations and financial condition|diluted eps|earnings per share|"
    r"revenue|net sales|guidance|outlook|operating margin|gross margin|backlog|bookings)\b",
    re.IGNORECASE,
)
_ITEM_202_SECTION_RE = re.compile(
    r"(item\s*2\.02\b.*?)(?=item\s*\d+\.\d+\b|signature(?:s)?\b)",
    re.IGNORECASE | re.DOTALL,
)

logger = logging.getLogger(__name__)


class SecClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {
            "User-Agent": settings.sec_user_agent,
            "Accept": "application/json",
        }
        self._llm_client = None
        if OpenAI is not None and settings.llm_base_url and settings.llm_api_key and settings.sec_summary_model:
            base = settings.llm_base_url.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            self._llm_client = OpenAI(base_url=base, api_key=settings.llm_api_key)

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

    def _fetch_html(self, client: httpx.Client, url: str) -> str:
        try:
            resp = client.get(url, headers={**self.headers, "Accept": "text/html,application/xhtml+xml"}, timeout=20.0)
            if resp.status_code != 200:
                return ""
            return resp.text
        except Exception:
            return ""

    @staticmethod
    def _html_to_text(html: str, max_chars: int = 8000) -> str:
        if not html:
            return ""
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    def _fetch_filing_text(self, client: httpx.Client, url: str, max_chars: int = 8000) -> str:
        return self._html_to_text(self._fetch_html(client, url), max_chars=max_chars)

    @staticmethod
    def _accession_from_text(text: str) -> str | None:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", text)
        return m.group(1) if m else None

    @staticmethod
    def _parse_published_at(acceptance_datetime: str | None, filing_date: str | None):
        for raw in (acceptance_datetime, filing_date):
            if not raw:
                continue
            try:
                return dt_parser.parse(str(raw))
            except Exception:
                continue
        return utc_now()

    @staticmethod
    def _filing_index_url(cik: str, accession: str) -> str:
        accession_plain = accession.replace("-", "")
        archive_cik = cik.lstrip("0") or "0"
        return f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_plain}/{accession}-index.html"

    def _extract_exhibit_991_url(self, index_html: str, index_url: str) -> str | None:
        if not index_html:
            return None
        for row in _EXHIBIT_991_ROW_RE.findall(index_html):
            match = _HREF_RE.search(row)
            if match:
                return urljoin(index_url, match.group(1))
        if "99.1" in index_html.lower():
            match = _HREF_RE.search(index_html)
            if match:
                return urljoin(index_url, match.group(1))
        return None

    @staticmethod
    def _extract_item_202_section(filing_text: str) -> str:
        match = _ITEM_202_SECTION_RE.search(filing_text or "")
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:10000]
        return ""

    def _looks_like_sec_earnings_release(self, form: str, filing_text: str, exhibit_text: str) -> bool:
        if form != "8-K":
            return False
        combined = f"{filing_text}\n{exhibit_text}".strip()
        if not combined:
            return False
        has_item_202 = bool(_ITEM_202_RE.search(filing_text or ""))
        has_earnings_text = bool(_SEC_EARNINGS_SIGNAL_RE.search(combined))
        has_exhibit_summary = bool(exhibit_text and _SEC_EARNINGS_SIGNAL_RE.search(exhibit_text))
        return (has_item_202 and has_earnings_text) or has_exhibit_summary

    @staticmethod
    def _parse_llm_json(content: str) -> dict:
        text = (content or "").strip()
        if not text:
            raise ValueError("empty llm content")
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fence_match:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("llm response did not contain json")

    def _fallback_earnings_summary(self, ticker: str, filing_text: str, exhibit_text: str) -> tuple[str, str]:
        primary = exhibit_text or self._extract_item_202_section(filing_text) or filing_text
        compact = re.sub(r"\s+", " ", primary).strip()
        headline = f"{ticker} SEC earnings release filed under 8-K Item 2.02"
        summary = compact[: max(180, int(self.settings.sec_summary_max_chars))]
        return headline[:220], summary

    def _summarize_sec_earnings(self, ticker: str, filing_text: str, exhibit_text: str) -> tuple[str, str]:
        fallback_headline, fallback_summary = self._fallback_earnings_summary(ticker, filing_text, exhibit_text)
        if self._llm_client is None:
            return fallback_headline, fallback_summary

        source_text = exhibit_text or self._extract_item_202_section(filing_text) or filing_text
        source_text = re.sub(r"\s+", " ", source_text).strip()[:24000]
        if not source_text:
            return fallback_headline, fallback_summary

        prompt = (
            "You summarize SEC 8-K earnings releases (Item 2.02 / Exhibit 99.1). "
            "Return JSON only with keys headline_en and summary_zh. "
            f"summary_zh must be <= {int(self.settings.sec_summary_max_chars)} Chinese characters, concise, and preserve exact numbers/units from the filing. "
            "Do not invent analyst estimates or market reaction if absent. "
            "If available, mention EPS actual/estimate, revenue actual/estimate, guidance, margin, capex, backlog, and management outlook. "
            "headline_en should be one short English line that still contains the core numbers or beat/miss/guidance direction."
        )
        try:
            resp = self._llm_client.chat.completions.create(
                model=self.settings.sec_summary_model,
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": f"Ticker: {ticker}\n\nSEC source text:\n{source_text}",
                    },
                ],
                max_tokens=900,
                temperature=0.0,
            )
            content = (resp.choices[0].message.content or "").strip()
            parsed = self._parse_llm_json(content)
            headline = str(parsed.get("headline_en") or "").strip()[:220]
            summary = str(parsed.get("summary_zh") or "").strip()[: int(self.settings.sec_summary_max_chars)]
            if headline and summary:
                return headline, summary
        except Exception as exc:
            logger.warning("SEC earnings summary LLM failed for %s: %s", ticker, exc)
        return fallback_headline, fallback_summary

    def _build_sec_earnings_item(
        self,
        *,
        ticker: str,
        cik: str,
        accession: str,
        filing_url: str,
        acceptance_datetime: str | None,
        filing_date: str | None,
        filing_text: str,
        exhibit_url: str | None,
        exhibit_text: str,
    ) -> RawNewsItem:
        published = self._parse_published_at(acceptance_datetime, filing_date)
        headline_en, summary_zh = self._summarize_sec_earnings(ticker, filing_text, exhibit_text)
        item_hash = make_hash("sec", f"{accession}:sec_earnings_release", ticker)
        source_text = exhibit_text or self._extract_item_202_section(filing_text) or filing_text
        source_text = source_text[:20000]
        body = (
            f"LLM_SUMMARY_ZH:\n{summary_zh}\n\n"
            f"SEC_HEADLINE_EN:\n{headline_en}\n\n"
            f"SEC_SOURCE_TEXT:\n{source_text}"
        )
        return RawNewsItem(
            source="sec",
            url=exhibit_url or filing_url,
            title=headline_en[:500] or f"{ticker} SEC earnings release",
            body=body,
            published_at=published,
            ingested_at=utc_now(),
            hash=item_hash,
            source_tier=0,
            metadata={
                "ticker": ticker,
                "cik": cik,
                "form": "8-K",
                "accession": accession,
                "filing_date": filing_date,
                "acceptance_datetime": acceptance_datetime,
                "event_type_hint": "sec_earnings_release",
                "sec_item_202": True,
                "exhibit_99_1_url": exhibit_url,
                "summary_override": summary_zh,
                "headline_en": headline_en,
                "summary_model": self.settings.sec_summary_model,
            },
        )

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
        sec_earnings_items = 0

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
                acceptance_datetimes = recent.get("acceptanceDateTime", [])
                accessions = recent.get("accessionNumber", [])
                docs = recent.get("primaryDocument", [])

                rows = zip(forms, filing_dates, acceptance_datetimes, accessions, docs, strict=False)
                for form, filing_date, acceptance_datetime, accession, doc in rows:
                    if form not in SUPPORTED_FORMS or not accession:
                        continue

                    accession_plain = accession.replace("-", "")
                    archive_cik = cik.lstrip("0") or "0"
                    doc_name = doc or f"{accession}-index.html"
                    filing_url = f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_plain}/{doc_name}"
                    published = self._parse_published_at(acceptance_datetime, filing_date)
                    title = f"{ticker} filed {form}"

                    filing_text = ""
                    if form in FETCH_BODY_FORMS:
                        filing_text = self._fetch_filing_text(client, filing_url, max_chars=20000 if form == "8-K" else 8000)
                        sleep(0.15)

                    if form == "8-K" and filing_text:
                        index_url = self._filing_index_url(cik, accession)
                        index_html = self._fetch_html(client, index_url)
                        exhibit_url = self._extract_exhibit_991_url(index_html, index_url)
                        exhibit_text = ""
                        if exhibit_url:
                            exhibit_text = self._fetch_filing_text(client, exhibit_url, max_chars=20000)
                            sleep(0.15)
                        if self._looks_like_sec_earnings_release(form, filing_text, exhibit_text):
                            items.append(
                                self._build_sec_earnings_item(
                                    ticker=ticker,
                                    cik=cik,
                                    accession=accession,
                                    filing_url=filing_url,
                                    acceptance_datetime=acceptance_datetime,
                                    filing_date=filing_date,
                                    filing_text=filing_text,
                                    exhibit_url=exhibit_url,
                                    exhibit_text=exhibit_text,
                                )
                            )
                            sec_earnings_items += 1
                            continue

                    if form in FETCH_BODY_FORMS:
                        body = filing_text or f"SEC filing {form} accession {accession}"
                    else:
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
                                "filing_date": filing_date,
                                "acceptance_datetime": acceptance_datetime,
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
                    "sec_earnings_items": sec_earnings_items,
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
                "sec_earnings_items": sec_earnings_items,
            },
        )
