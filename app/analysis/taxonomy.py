"""Event taxonomy and simple keyword inference for V1."""

from __future__ import annotations

import re

EVENT_KEYWORDS = {
    "financial_fraud": ["fraud", "restatement", "misstatement", "accounting irregular"],
    "audit_issue": ["audit", "auditor resignation", "material weakness", "internal control"],
    "earnings_miss": ["missed estimates", "earnings miss", "below expectations"],
    "sec_earnings_release": ["item 2.02", "exhibit 99.1", "earnings release", "quarterly results"],
    "guidance_cut": ["guidance cut", "lowered outlook", "cuts forecast", "warned"],
    "regulatory_penalty": ["fine", "penalty", "sec charge", "doj", "sec settlement", "doj settlement", "civil penalty"],
    "major_litigation": ["lawsuit", "litigation", "class action", "court ruling"],
    "merger_acquisition": ["acquire", "acquisition", "merger", "takeover", "buyout", "acquires"],
    "buyback": ["buyback", "repurchase", "share repurchase"],
    "layoff": ["layoff", "job cuts", "workforce reduction"],
    "supply_chain_disruption": ["supply chain", "supply-chain", "disruption", "plant shutdown", "factory shutdown", "port shutdown", "production halt", "delay"],
    "accident_disaster": ["fire", "explosion", "accident", "outage", "earthquake"],
    "policy_shock": ["tariff", "sanction", "ban", "policy shock", "executive order"],
    "sec_filing": ["filed 10-k", "filed 10-q", "filed 8-k", "filed 6-k", "filed 13d", "filed 13g", "annual report", "quarterly report"],
}

POSITIVE_EVENTS = {"buyback", "merger_acquisition"}
NEGATIVE_EVENTS = {
    "financial_fraud",
    "audit_issue",
    "earnings_miss",
    "guidance_cut",
    "regulatory_penalty",
    "major_litigation",
    "layoff",
    "supply_chain_disruption",
    "accident_disaster",
    "policy_shock",
}

# sec_filing: routine filings, no directional edge
# unknown: unclassified news (mostly generic Finnhub company-news), too noisy
# supply_chain_disruption: taxonomy too broad; catches government shutdown / general macro articles
# guidance_cut: keyword "warned" too loose; catches unrelated warnings; body usually too short to trade
EXCLUDED_FROM_TRADING = {"sec_filing", "unknown", "supply_chain_disruption", "guidance_cut"}

SECONDARY_CONFIRMATION_SOURCES = frozenset({"cnbc", "yahoo", "yahoo_finance"})

_SOURCE_ALIASES = {
    "yahoo": "yahoo_finance",
    "yahoo finance": "yahoo_finance",
    "yahoo-finance": "yahoo_finance",
}

SOURCE_TIER = {
    "sec": 0,
    "exchange": 0,
    "company": 0,
    "earnings_release": 0,
    "reuters": 1,
    "bloomberg": 1,
    "wsj": 1,
    "ft": 1,
    "nytimes": 1,
    "cnbc": 2,
    "bbc": 1,
    "aljazeera": 1,
    "axios": 1,
    "npr": 1,
    "marketwatch": 2,
    "yahoo": 2,
    "yahoo_finance": 2,
    "thestreet": 2,
    "seekingalpha": 2,
    "finnhub": 2,
    "rss": 2,
}

TIER_SCORE = {
    0: 50,
    1: 30,
    2: 15,
}

_POSITIVE_EARNINGS_RE = re.compile(
    r"\b(guides?\s+above|above (?:q\d\s+)?estimates|beats? (?:estimates|expectations)"
    r"|tops? estimates|raises? (?:guidance|forecast|outlook)|higher sales|sales (?:rise|rose|up)"
    r"|revenue(?:s)? (?:rise|rose|up)|profit (?:rise|rose|up)|stock (?:rose|jumped|surged)"
    r"|shares? (?:rose|jumped|surged)|better than expected|strong demand)\b",
    re.IGNORECASE,
)
_NEGATIVE_EARNINGS_RE = re.compile(
    r"\b(missed? estimates|earnings miss|below expectations|below estimates|cuts? (?:forecast|outlook|guidance)"
    r"|warned on|soft demand|weaker than expected)\b",
    re.IGNORECASE,
)
_REGULATORY_CONTEXT_RE = re.compile(
    r"\b(sec|doj|penalt(?:y|ies)|fine[sd]?|charge[sd]?|regulator(?:y)?|enforcement|civil penalty|consent order)\b",
    re.IGNORECASE,
)
_POSITIVE_RESOLUTION_RE = re.compile(
    r"\b(settles? litigation|settlement with|resolves? litigation|wins? (?:case|appeal)"
    r"|complete victory|dismissed lawsuit|extends? .* deal|stock rose|shares? rose"
    r"|higher sales|sooner than expected|spin[\s-]?off|maintenance deal"
    r"|favorable court ruling|positive outlook|settle ai lawsuits)\b",
    re.IGNORECASE,
)
_NEGATIVE_LITIGATION_RE = re.compile(
    r"\b(class action|lawsuit filed|sued by|court ruling against|legal challenge|appeal denied|trial)\b",
    re.IGNORECASE,
)
_WEAK_LITIGATION_CONTEXT_RE = re.compile(
    r"\b(what it means|positive outlook|following favorable court ruling|eye investor funds to settle"
    r"|plans? to settle|weighs? settlement options|could settle|settlement talks)\b",
    re.IGNORECASE,
)
_EARNINGS_WINDOW_RE = re.compile(
    r"\b(earnings|eps|estimate(?:s)?|guid(?:e|ance)|forecast|outlook|quarter|q[1-4]|revenue|sales)\b",
    re.IGNORECASE,
)
_PRICE_RECAP_RE = re.compile(
    r"\b(how .* stock (?:jumped|rose|fell|dropped|surged|slid)|stock jumped|stock rose|stock fell"
     r"|shares? jumped|shares? rose|shares? fell)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_COMMENTARY_RE = re.compile(
    r"\b(trade tracker|final trades|what to know|what it means|market chatter|market commentary|commentary piece"
    r"|column|opinion|price target|analyst (?:says|call|note|view)|technical analysis"
    r"|ready for (?:a )?\d+% surge|ready for a surge|buy here|returns to haunt"
    r"|long[\s-]?term potential|preview:|q[1-4] preview|earnings preview"
    r"|does that make .* a buy|what investors need to know|what'?s going on with"
    r"|why are .* (?:shares|stock) trading|why .* stock (?:is|was|keeps) (?:up|down|moving)"
    r"|shares? (?:are|were) trading (?:higher|lower)|stocks making the biggest moves|biggest movers"
    r"|top movers|roundup|recap|market recap|opening bell|ahead of the bell|after the bell)\b",
    re.IGNORECASE,
)


def normalize_source_name(source: str | None) -> str:
    lowered = (source or "").strip().lower()
    if not lowered:
        return ""
    alias = _SOURCE_ALIASES.get(lowered)
    if alias:
        return alias
    compact = re.sub(r"\s+", "_", lowered)
    compact = compact.replace("-", "_")
    compact = re.sub(r"_+", "_", compact).strip("_")
    return _SOURCE_ALIASES.get(compact, compact)


def is_secondary_confirmation_source(source: str | None) -> bool:
    return normalize_source_name(source) in SECONDARY_CONFIRMATION_SOURCES


def is_follow_up_commentary(text: str | None) -> bool:
    return bool(_FOLLOW_UP_COMMENTARY_RE.search(text or ""))


def resolve_event_type_for_text(event_type: str | None, text: str) -> str:
    et = (event_type or "unknown").strip()
    lowered = text or ""

    if not lowered:
        return et

    if is_follow_up_commentary(lowered) and et not in {"sec_earnings_release", "sec_filing"}:
        return "unknown"

    if et == "earnings_miss":
        has_positive = bool(_POSITIVE_EARNINGS_RE.search(lowered))
        has_negative = bool(_NEGATIVE_EARNINGS_RE.search(lowered))
        if has_positive and not has_negative:
            return "unknown"
        if has_positive and has_negative:
            return "unknown"

    if et == "regulatory_penalty":
        if not _REGULATORY_CONTEXT_RE.search(lowered):
            if any(token in lowered.lower() for token in ("litigation", "lawsuit", "court")):
                et = "major_litigation"
            else:
                return "unknown"
        if _POSITIVE_RESOLUTION_RE.search(lowered):
            return "unknown"

    if et == "major_litigation":
        if (
            _POSITIVE_RESOLUTION_RE.search(lowered)
            or _WEAK_LITIGATION_CONTEXT_RE.search(lowered)
        ) and not _NEGATIVE_LITIGATION_RE.search(lowered):
            return "unknown"

    return et


def is_earnings_window_event(event_type: str | None, text: str) -> bool:
    et = resolve_event_type_for_text(event_type, text)
    return et in {"earnings_miss", "guidance_cut", "sec_earnings_release"} or bool(_EARNINGS_WINDOW_RE.search(text or ""))


def is_price_action_recap(text: str) -> bool:
    return bool(_PRICE_RECAP_RE.search(text or ""))
