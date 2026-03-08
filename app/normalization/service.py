from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import EVENT_KEYWORDS
from app.core.company_names import COMPANY_NAME_TO_TICKER
from app.core.config import Settings
from app.core.utils import minute_bucket
from app.db.models import RawItem
from app.schemas.types import CanonicalEvent

logger = logging.getLogger(__name__)

_VALID_EVENT_TYPES = set(EVENT_KEYWORDS.keys()) | {"unknown"}

_CLASSIFIER_PROMPT = """You are a financial news classifier. Classify the following news text into exactly one event type.

Event types:
- financial_fraud: fraud, restatement, accounting irregularities
- audit_issue: auditor issues, material weakness, internal control failures
- earnings_miss: missed earnings estimates, below expectations
- guidance_cut: lowered guidance, cut forecast, warned on outlook
- regulatory_penalty: fines, penalties, SEC charges, DOJ, settlements
- major_litigation: lawsuits, class actions, court rulings
- merger_acquisition: acquisitions, mergers, takeovers, deals
- buyback: share buybacks, repurchases
- layoff: layoffs, job cuts, workforce reductions
- supply_chain_disruption: supply chain issues, shutdowns, delays
- accident_disaster: fires, explosions, accidents, outages
- policy_shock: tariffs, sanctions, executive orders, policy shocks
- sec_filing: routine SEC filings (10-K, 10-Q, 8-K, 13D, 13G)
- unknown: does not fit any above category

Reply with ONLY the event type label, nothing else."""


@dataclass
class NormalizedCluster:
    canonical: CanonicalEvent
    raw_items: list[RawItem]


class NormalizationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.universe = set(settings.sp100_tickers)
        # Build sorted list of company names (longest first to avoid partial matches)
        self._name_map = sorted(COMPANY_NAME_TO_TICKER.keys(), key=len, reverse=True)
        self._llm_client: OpenAI | None = None
        if settings.llm_base_url and settings.llm_api_key:
            base = settings.llm_base_url.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            self._llm_client = OpenAI(base_url=base, api_key=settings.llm_api_key)

    def _extract_tickers(self, text: str, metadata_json: dict) -> list[str]:
        found = []
        seen = set()

        # 1. Token-based symbol match (e.g. "AAPL", "MSFT")
        tokens = text.replace("$", " ").replace(",", " ").replace(".", ". ").split()
        for token in tokens:
            cleaned = token.strip().upper()
            if cleaned in self.universe and cleaned not in seen:
                found.append(cleaned)
                seen.add(cleaned)

        # 2. Company name substring match (word-boundary aware, longest first)
        lowered = text.lower()
        for name in self._name_map:
            ticker = COMPANY_NAME_TO_TICKER[name]
            if ticker in seen:
                continue
            # Use word boundary: name must not be part of a larger word
            pattern = r"(?<![a-z])" + re.escape(name) + r"(?![a-z])"
            if re.search(pattern, lowered):
                if ticker in self.universe:
                    found.append(ticker)
                    seen.add(ticker)

        # 3. Metadata hint (e.g. SEC filings carry explicit ticker)
        hint = (metadata_json or {}).get("ticker")
        if hint and hint in self.universe and hint not in seen:
            found.append(hint)
            seen.add(hint)

        return found

    def _keyword_classify(self, text: str) -> str:
        lowered = text.lower()
        for event_type, keywords in EVENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in lowered:
                    return event_type
        return "unknown"

    def _llm_classify(self, text: str) -> str:
        if not self._llm_client:
            return "unknown"
        snippet = text[:1200]
        try:
            resp = self._llm_client.chat.completions.create(
                model=self.settings.llm_classifier_model,
                messages=[
                    {"role": "system", "content": _CLASSIFIER_PROMPT},
                    {"role": "user", "content": snippet},
                ],
                max_tokens=16,
                temperature=0.0,
            )
            label = resp.choices[0].message.content.strip().lower().replace("-", "_")
            if label in _VALID_EVENT_TYPES:
                return label
            logger.warning("LLM classifier returned unknown label: %r", label)
        except Exception as exc:
            logger.warning("LLM classifier failed: %s", exc)
        return "unknown"

    def _infer_event_type(self, text: str) -> str:
        # Keyword pass first (fast, free)
        result = self._keyword_classify(text)
        if result != "unknown":
            return result
        # Fall back to LLM classifier for ambiguous items
        return self._llm_classify(text)

    def _severity(self, event_type: str) -> int:
        severe = {"financial_fraud", "audit_issue", "regulatory_penalty", "accident_disaster"}
        mid = {"earnings_miss", "guidance_cut", "major_litigation", "supply_chain_disruption"}
        if event_type in severe:
            return 85
        if event_type in mid:
            return 70
        return 55

    def build_clusters(self, session: Session, raw_ids: Iterable[int] | None = None) -> list[NormalizedCluster]:
        stmt = select(RawItem).where(RawItem.processed.is_(False))
        if raw_ids:
            stmt = stmt.where(RawItem.id.in_(list(raw_ids)))
        rows = session.execute(stmt.order_by(RawItem.published_at.asc())).scalars().all()

        grouped: dict[tuple[str, str, str], NormalizedCluster] = {}
        for item in rows:
            text = f"{item.title} {item.body}"
            tickers = self._extract_tickers(text, item.metadata_json)
            # Skip slow LLM classifier for ticker-tagged items (Finnhub company-news);
            # the main analysis LLM reads full text to determine direction anyway.
            has_ticker_meta = bool((item.metadata_json or {}).get("ticker"))
            if has_ticker_meta:
                event_type = self._keyword_classify(text)
                # keep "unknown" without calling LLM — main analysis LLM handles direction
            else:
                event_type = self._infer_event_type(text)
            primary_ticker = tickers[0] if tickers else "UNKNOWN"
            bucket = minute_bucket(item.published_at, width_min=30).isoformat()
            key = (primary_ticker, event_type, bucket)

            if key not in grouped:
                grouped[key] = NormalizedCluster(
                    canonical=CanonicalEvent(
                        event_type=event_type,
                        entities=tickers,
                        tickers=tickers,
                        severity=self._severity(event_type),
                        event_time=item.published_at,
                        evidence_refs=[item.id],
                        summary=item.title[:280],
                    ),
                    raw_items=[item],
                )
            else:
                group = grouped[key]
                group.raw_items.append(item)
                group.canonical.evidence_refs.append(item.id)
                for t in tickers:
                    if t not in group.canonical.tickers:
                        group.canonical.tickers.append(t)
                        group.canonical.entities.append(t)
                if item.published_at < group.canonical.event_time:
                    group.canonical.event_time = item.published_at

        return list(grouped.values())
