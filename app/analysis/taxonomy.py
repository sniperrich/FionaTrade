"""Event taxonomy and simple keyword inference for V1."""

EVENT_KEYWORDS = {
    "financial_fraud": ["fraud", "restatement", "misstatement", "accounting irregular"],
    "audit_issue": ["audit", "auditor resignation", "material weakness", "internal control"],
    "earnings_miss": ["missed estimates", "earnings miss", "below expectations"],
    "guidance_cut": ["guidance cut", "lowered outlook", "cuts forecast", "warned"],
    "regulatory_penalty": ["fine", "penalty", "sec charge", "doj", "settlement"],
    "major_litigation": ["lawsuit", "litigation", "class action", "court ruling"],
    "merger_acquisition": ["acquire", "acquisition", "merger", "takeover", "deal"],
    "buyback": ["buyback", "repurchase", "share repurchase"],
    "layoff": ["layoff", "job cuts", "workforce reduction"],
    "supply_chain_disruption": ["supply chain", "disruption", "shutdown", "delay"],
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
EXCLUDED_FROM_TRADING = {"sec_filing", "unknown"}

SOURCE_TIER = {
    "sec": 0,
    "exchange": 0,
    "company": 0,
    "reuters": 1,
    "bloomberg": 1,
    "wsj": 1,
    "ft": 1,
    "cnbc": 1,
    "marketwatch": 2,
    "seekingalpha": 2,
    "finnhub": 2,
    "rss": 2,
}

TIER_SCORE = {
    0: 50,
    1: 30,
    2: 15,
}
