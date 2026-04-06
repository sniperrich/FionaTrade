from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency fallback
    OpenAI = None
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.taxonomy import EVENT_KEYWORDS, normalize_source_name, resolve_event_type_for_text
from app.core.company_names import COMPANY_NAME_TO_TICKER, TICKER_TO_COMPANY_ALIASES
from app.core.config import Settings
from app.core.utils import ensure_utc, minute_bucket
from app.db.models import RawItem
from app.schemas.types import CanonicalEvent

logger = logging.getLogger(__name__)

_VALID_EVENT_TYPES = set(EVENT_KEYWORDS.keys()) | {"unknown"}
_ROUTINE_FILING_RE = re.compile(
    r"\bfiled\s+(?:form\s+)?(?:8-k|10-k|10-q|6-k|13d|13g|sc\s*13d|sc\s*13g)\b",
    re.IGNORECASE,
)
_ROUTINE_FILING_MARKERS = (
    "sec filing",
    "form 8-k",
    "form 10-k",
    "form 10-q",
    "form 6-k",
    "form 13d",
    "form 13g",
)
_MATERIAL_FILING_KEYWORDS = (
    "restatement",
    "material weakness",
    "internal control",
    "bankrupt",
    "chapter 11",
    "investigation",
    "sec charge",
    "doj",
    "fraud",
    "guidance cut",
    "lowered outlook",
    "earnings miss",
    "missed estimates",
)

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

_NORMALIZER_PROMPT = """You are refining normalized financial news for an event-driven trading system.

Return strict JSON with these keys:
- primary_ticker: uppercase ticker string or ""
- related_tickers: list of uppercase ticker strings
- event_type: one of financial_fraud, audit_issue, earnings_miss, sec_earnings_release, guidance_cut, regulatory_penalty, major_litigation, merger_acquisition, buyback, layoff, supply_chain_disruption, accident_disaster, policy_shock, sec_filing, unknown
- is_ticker_specific: boolean
- is_material_new_information: boolean
- is_follow_up_commentary: boolean
- is_price_action_explanation: boolean
- merge_key: short snake_case event key or ""
- summary: <= 240 chars, concrete factual summary

Rules:
- Prefer the primary ticker actually impacted by the article, not every company mentioned.
- Distinguish hard catalysts from commentary. Hard catalysts include concrete company-specific facts such as earnings, guidance, deliveries, regulatory approvals, permanent billing/reimbursement codes, lawsuits filed, M&A, financing, contract wins/losses, executive departures, penalties, or formally reported operational incidents.
- If a headline contains BOTH price-action wording and a new hard catalyst ("shares fall after disappointing deliveries report", "stock jumps on FDA approval"), treat it as material new information and not as a mere price-action explanation.
- Do not mark an item as follow-up commentary just because it says "reported earlier" if it still states a concrete company-specific catalyst that would matter intraday.
- Mark follow-up commentary, roundup, analyst chatter, peer comparisons, "final trades", "stocks moving", and pure "why stock is moving/falling" explainers as not material new information when they do not add a fresh hard catalyst.
- Use unknown when the article is not a clear company-specific tradable catalyst.
- Prefer a single primary ticker. Use related_tickers only when the article genuinely affects multiple named companies; do not keep passive mentions as co-equal tickers.
- merge_key should group different source phrasings of the same underlying event."""


@dataclass
class NormalizedCluster:
    canonical: CanonicalEvent
    raw_items: list[RawItem]


@dataclass
class NormalizationLLMRefinement:
    primary_ticker: str | None = None
    related_tickers: list[str] | None = None
    event_type: str | None = None
    is_ticker_specific: bool = True
    is_material_new_information: bool = True
    is_follow_up_commentary: bool = False
    is_price_action_explanation: bool = False
    merge_key: str | None = None
    summary: str | None = None


class NormalizationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.universe = set(settings.sp100_tickers)
        # Build sorted list of company names (longest first to avoid partial matches)
        self._name_map = sorted(COMPANY_NAME_TO_TICKER.keys(), key=len, reverse=True)
        self._ticker_aliases = {
            ticker: aliases for ticker, aliases in TICKER_TO_COMPANY_ALIASES.items() if ticker in self.universe
        }
        self._llm_client = None
        self._refinement_cache: dict[str, NormalizationLLMRefinement | None] = {}
        if OpenAI is not None and settings.llm_base_url and settings.llm_api_key:
            base = settings.llm_base_url.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            self._llm_client = OpenAI(base_url=base, api_key=settings.llm_api_key)

    def _text_mentions_ticker(self, text: str, ticker: str) -> bool:
        lowered = (text or "").lower()
        tokens = text.replace("$", " ").replace(",", " ").replace(".", ". ").split()
        for token in tokens:
            if token.strip().upper() == ticker:
                return True

        for alias in self._ticker_aliases.get(ticker, ()):
            pattern = r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])"
            if re.search(pattern, lowered):
                return True
        return False

    @staticmethod
    def _is_structured_ticker_source(source: str, metadata_json: dict) -> bool:
        lowered_source = (source or "").lower().strip()
        if lowered_source in {"sec", "earnings_release"}:
            return True
        return bool((metadata_json or {}).get("structured_ticker"))

    def _extract_tickers(self, text: str, metadata_json: dict, source: str = "") -> list[str]:
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
        hint = str((metadata_json or {}).get("ticker") or "").upper().strip()
        if hint and hint in self.universe and hint not in seen:
            if self._is_structured_ticker_source(source, metadata_json) or self._text_mentions_ticker(text, hint):
                found.append(hint)
                seen.add(hint)
            else:
                logger.info(
                    "Dropped unverified metadata ticker hint source=%s hint=%s title_text=%r",
                    source,
                    hint,
                    (text or "")[:160],
                )

        return found

    @staticmethod
    def _item_summary(item: RawItem) -> str:
        override = str((item.metadata_json or {}).get("summary_override") or "").strip()
        if override:
            return override[:1000]
        return (item.title or "")[:280]

    def _keyword_classify(self, text: str) -> str:
        lowered = text.lower()
        for event_type, keywords in EVENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in lowered:
                    return resolve_event_type_for_text(event_type, lowered)
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
        return resolve_event_type_for_text(self._llm_classify(text), text)

    def _is_routine_filing_item(self, item: RawItem, text: str) -> bool:
        title = (item.title or "").lower()
        lowered = text.lower()
        source = (item.source or "").lower()

        has_filing_marker = bool(_ROUTINE_FILING_RE.search(title)) or any(
            marker in lowered for marker in _ROUTINE_FILING_MARKERS
        )
        if not has_filing_marker:
            return False

        if source != "sec" and "filed" not in title:
            return False

        has_material_marker = any(keyword in lowered for keyword in _MATERIAL_FILING_KEYWORDS)
        return not has_material_marker

    def _severity(self, event_type: str) -> int:
        severe = {"financial_fraud", "audit_issue", "regulatory_penalty", "accident_disaster"}
        mid = {"earnings_miss", "guidance_cut", "major_litigation", "supply_chain_disruption"}
        if event_type in severe:
            return 85
        if event_type in mid:
            return 70
        return 55

    @staticmethod
    def _sanitize_merge_key(value: str | None) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        cleaned = re.sub(r"[^a-z0-9]+", "_", raw)
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        if not cleaned:
            return None
        return cleaned[:96]

    def _should_refine_with_llm(self, item: RawItem) -> bool:
        if not getattr(self.settings, "normalization_llm_enabled", False):
            return False
        if self._llm_client is None:
            return False
        allowed_sources = {
            normalize_source_name(source)
            for source in getattr(self.settings, "normalization_llm_sources", [])
        }
        if not allowed_sources:
            return False
        if normalize_source_name(item.source) not in allowed_sources:
            return False
        return bool((item.title or "").strip() or (item.body or "").strip())

    def _parse_llm_json(self, content: str | None) -> dict | None:
        if not content:
            return None
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Normalization LLM returned invalid JSON: %r", stripped[:240])
            return None
        return parsed if isinstance(parsed, dict) else None

    def _maybe_refine_with_llm(
        self,
        item: RawItem,
        text: str,
        tickers: list[str],
        event_type: str,
    ) -> NormalizationLLMRefinement | None:
        if not self._should_refine_with_llm(item):
            return None

        cache_key = (
            f"{getattr(self.settings, 'normalization_llm_prompt_version', 'v1')}"
            f"|{item.item_hash}|{event_type}|{','.join(tickers)}"
        )
        if cache_key in self._refinement_cache:
            return self._refinement_cache[cache_key]

        snippet = {
            "source": item.source,
            "source_tier": int(item.source_tier or 9),
            "initial_tickers": tickers,
            "initial_event_type": event_type,
            "metadata_ticker": str((item.metadata_json or {}).get("ticker") or "").upper().strip(),
            "title": item.title or "",
            "body": (item.body or "")[: int(getattr(self.settings, "normalization_llm_max_chars", 2400))],
        }
        if getattr(self.settings, "llm_merge_system_prompt", True):
            messages = [
                {
                    "role": "user",
                    "content": f"{_NORMALIZER_PROMPT}\n\n---\n\n{json.dumps(snippet, ensure_ascii=True)}",
                }
            ]
        else:
            messages = [
                {"role": "system", "content": _NORMALIZER_PROMPT},
                {"role": "user", "content": json.dumps(snippet, ensure_ascii=True)},
            ]
        try:
            resp = self._llm_client.chat.completions.create(
                model=getattr(self.settings, "llm_normalization_model", self.settings.llm_classifier_model),
                messages=messages,
                temperature=0.0,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            payload = self._parse_llm_json(resp.choices[0].message.content)
        except Exception as exc:
            logger.warning("Normalization LLM refinement failed for raw_item=%s: %s", item.id, exc)
            self._refinement_cache[cache_key] = None
            return None

        if not payload:
            self._refinement_cache[cache_key] = None
            return None

        primary_ticker = str(payload.get("primary_ticker") or "").upper().strip()
        if primary_ticker and primary_ticker not in self.universe:
            primary_ticker = ""

        related: list[str] = []
        for raw_ticker in payload.get("related_tickers") or []:
            ticker = str(raw_ticker or "").upper().strip()
            if ticker and ticker in self.universe and ticker not in related and ticker != primary_ticker:
                related.append(ticker)

        refined_event_type = str(payload.get("event_type") or "").strip().lower().replace("-", "_")
        if refined_event_type not in _VALID_EVENT_TYPES:
            refined_event_type = None

        refinement = NormalizationLLMRefinement(
            primary_ticker=primary_ticker or None,
            related_tickers=related,
            event_type=refined_event_type,
            is_ticker_specific=bool(payload.get("is_ticker_specific", True)),
            is_material_new_information=bool(payload.get("is_material_new_information", True)),
            is_follow_up_commentary=bool(payload.get("is_follow_up_commentary", False)),
            is_price_action_explanation=bool(payload.get("is_price_action_explanation", False)),
            merge_key=self._sanitize_merge_key(payload.get("merge_key")),
            summary=str(payload.get("summary") or "").strip()[:240] or None,
        )
        self._refinement_cache[cache_key] = refinement
        return refinement

    def _apply_llm_refinement(
        self,
        item: RawItem,
        tickers: list[str],
        event_type: str,
        summary: str,
        refinement: NormalizationLLMRefinement | None,
    ) -> tuple[list[str], str, str, str | None]:
        if refinement is None:
            return tickers, event_type, summary, None

        is_structured = self._is_structured_ticker_source(item.source, item.metadata_json)
        refined_tickers = list(tickers)
        refined_event_type = event_type
        refined_summary = refinement.summary or summary

        if not refinement.is_ticker_specific and not is_structured:
            refined_tickers = []
        elif refinement.primary_ticker:
            ordered = [refinement.primary_ticker]
            for ticker in refinement.related_tickers or []:
                if ticker not in ordered:
                    ordered.append(ticker)
            if is_structured:
                for ticker in tickers:
                    if ticker not in ordered:
                        ordered.append(ticker)
            refined_tickers = ordered

        if refinement.event_type:
            refined_event_type = resolve_event_type_for_text(refinement.event_type, f"{item.title} {item.body}")
        if (
            refinement.is_follow_up_commentary
            or refinement.is_price_action_explanation
            or not refinement.is_material_new_information
        ) and refined_event_type not in {"sec_filing", "sec_earnings_release"}:
            refined_event_type = "unknown"

        return refined_tickers, refined_event_type, refined_summary, refinement.merge_key

    def build_clusters(self, session: Session, raw_ids: Iterable[int] | None = None) -> list[NormalizedCluster]:
        stmt = select(RawItem).where(RawItem.processed.is_(False))
        rows: list[RawItem]
        if raw_ids:
            resolved_ids = [int(raw_id) for raw_id in raw_ids]
            rows = []
            for offset in range(0, len(resolved_ids), 500):
                chunk = resolved_ids[offset:offset + 500]
                chunk_rows = session.execute(
                    stmt.where(RawItem.id.in_(chunk)).order_by(RawItem.published_at.asc())
                ).scalars().all()
                rows.extend(chunk_rows)
            rows.sort(key=lambda item: ensure_utc(item.published_at))
        else:
            rows = session.execute(stmt.order_by(RawItem.published_at.asc())).scalars().all()
        merge_window_min = max(0, int(getattr(self.settings, "normalization_merge_window_min", 0)))

        grouped: dict[tuple[object, ...], NormalizedCluster] = {}
        for item in rows:
            text = f"{item.title} {item.body}"
            tickers = self._extract_tickers(text, item.metadata_json, source=item.source)
            if self._is_routine_filing_item(item, text):
                event_type = "sec_filing"
            else:
                hinted_event_type = str((item.metadata_json or {}).get("event_type_hint") or "").strip().lower()
                if hinted_event_type in _VALID_EVENT_TYPES:
                    event_type = resolve_event_type_for_text(hinted_event_type, text)
                else:
                    # Skip slow LLM classifier for ticker-tagged items (Finnhub company-news);
                    # the main analysis LLM reads full text to determine direction anyway.
                    has_ticker_meta = bool((item.metadata_json or {}).get("ticker"))
                    if has_ticker_meta:
                        event_type = self._keyword_classify(text)
                        # keep "unknown" without calling LLM — main analysis LLM handles direction
                    else:
                        event_type = self._infer_event_type(text)
            summary = self._item_summary(item)
            refinement = self._maybe_refine_with_llm(item, text, tickers, event_type)
            tickers, event_type, summary, merge_key = self._apply_llm_refinement(
                item=item,
                tickers=tickers,
                event_type=event_type,
                summary=summary,
                refinement=refinement,
            )
            primary_ticker = tickers[0] if tickers else "UNKNOWN"
            if merge_window_min > 0:
                bucket = minute_bucket(item.published_at, width_min=merge_window_min).isoformat()
                key = (primary_ticker, event_type, merge_key or event_type, bucket)
            else:
                key = (item.id,)

            if key not in grouped:
                grouped[key] = NormalizedCluster(
                    canonical=CanonicalEvent(
                        event_type=event_type,
                        entities=tickers,
                        tickers=tickers,
                        severity=self._severity(event_type),
                        event_time=ensure_utc(item.published_at),
                        evidence_refs=[item.id],
                        summary=summary,
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
                # If merge is enabled, event_time must represent the last evidence that was
                # already known to the system. Otherwise the merged cluster would leak future
                # evidence into an earlier tradable timestamp.
                item_ts = ensure_utc(item.published_at)
                if item_ts > ensure_utc(group.canonical.event_time):
                    group.canonical.event_time = item_ts
                    group.canonical.summary = summary

        return list(grouped.values())
