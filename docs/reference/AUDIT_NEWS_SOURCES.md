# FionaTrade News Data Sources Audit

## 1. INGESTION SOURCES (app/ingestion/)

### A. Enabled Data Sources (config.py)
```python
enable_sec: bool = True
enable_rss: bool = True
enable_finnhub: bool = True
enable_earnings_release_source: bool = True
```

### B. RSS Feed Sources (7 configured feeds)
**Config Location:** `app/core/config.py`, lines 115-125

```python
rss_sources: list[str] = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.marketwatch.com/rss/topstories",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://apnews.com/hub/financial-markets?format=rss",
    "https://seekingalpha.com/market_currents.xml",
]
```

### C. Ingestion Clients (app/ingestion/service.py)

**IngestionService._collect()** assembles items from 5 sources:

| Client | File | Description |
|--------|------|-------------|
| **SecClient** | sec_client.py | SEC filings (8-K, 10-Q, 10-K, 6-K, 13D, 13G) - **Tier 0** |
| **RssClient** | rss_client.py | 7 RSS feeds - **Tier 1-3 based on relevance** |
| **FinnhubNewsClient** | finnhub_client.py | General + company-specific news - **Tier 1-2** |
| **EarningsReleaseClient** | earnings_release_client.py | Structured earnings releases - **Tier 0** |
| **FREDClient** | fred_client.py | Macro indicators (CPI, GDP, unemployment, etc.) |

### D. Source Tiers (app/analysis/taxonomy.py, lines 44-58)

```python
SOURCE_TIER = {
    # Tier 0: Authoritative structured sources
    "sec": 0,
    "exchange": 0,
    "company": 0,
    "earnings_release": 0,
    
    # Tier 1: Major news outlets
    "reuters": 1,
    "bloomberg": 1,
    "wsj": 1,
    "ft": 1,
    "cnbc": 1,
    
    # Tier 2: Secondary news/research
    "marketwatch": 2,
    "seekingalpha": 2,
    "finnhub": 2,
    "rss": 2,
}

TIER_SCORE = {
    0: 50,  # Highest reliability score
    1: 30,  # Medium reliability
    2: 15,  # Lower reliability
}
```

---

## 2. NEWS FLOW ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────────┐
│ INGESTION LAYER (app/ingestion/)                                    │
│ ├─ SecClient.fetch()            → RawItem(processed=False)         │
│ ├─ RssClient.fetch()             → RawItem(processed=False)        │
│ ├─ FinnhubNewsClient.fetch()     → RawItem(processed=False)        │
│ └─ EarningsReleaseClient.fetch() → RawItem(processed=False)        │
└────────────────────┬────────────────────────────────────────────────┘
                     │ IngestionService.persist_items()
                     │ - Deduplication (title hash + URL + item_hash)
                     │ - 6-hour recency window for duplicates
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ RAW STORAGE (RawItem table)                                          │
│ Fields: source, source_tier, url, title, body, published_at,       │
│         ingested_at, item_hash, metadata_json, processed=False     │
└────────────────────┬────────────────────────────────────────────────┘
                     │ NormalizationService.build_clusters()
                     │ - Extract tickers (symbol match + company names)
                     │ - Classify event type (keywords → LLM if uncertain)
                     │ - Filter routine filings
                     │ - Merge within window (configurable)
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ NORMALIZED CLUSTERS (NormalizedCluster objects)                      │
│ - canonical: CanonicalEvent with event_type, tickers, severity    │
│ - raw_items: List of RawItem evidence                              │
└────────────────────┬────────────────────────────────────────────────┘
                     │ ValidationService.validate_and_store()
                     │ - Score confidence based on:
                     │   * Source count & tier quality
                     │   * Corroboration within 180min window
                     │   * Conflict detection (upgrade + downgrade)
                     │ - Status: VALID | WATCH | REJECTED
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ EVENT STORAGE (Event + EventEvidence tables)                        │
│ Event fields: event_type, tickers, severity, confidence,           │
│              validation_status, conflict_reason, summary,          │
│              event_time, created_at, signaled                      │
└────────────────────┬────────────────────────────────────────────────┘
                     │ NewsSentimentAgent.analyze()
                     │ - Query recent events (14 days = 336 hours)
                     │ - Apply to_of filter for backtesting
                     │ - Feed to LLM for sentiment/direction
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ AGENT DECISION (signal: BUY | SHORT | HOLD)                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. DATA MODELS

### A. RawItem Table (app/db/models.py, lines 26-40)

| Field | Type | Index | Notes |
|-------|------|-------|-------|
| id | Integer | PK | Auto-increment |
| source | String(64) | ✓ | e.g., "reuters", "finnhub", "sec" |
| source_tier | Integer | | 0=Tier0, 1=Tier1, 2=Tier2 |
| url | String(1024) | UNIQUE | Unique constraint |
| title | String(512) | | Article headline |
| body | Text | | Full article or summary |
| published_at | DateTime | ✓ | Original publication time |
| ingested_at | DateTime | ✓ | When we fetched it |
| item_hash | String(64) | UNIQUE ✓ | SHA hash for deduplication |
| metadata_json | JSON | | Source-specific metadata |
| processed | Boolean | ✓ | False until normalization |

**Composite Index:** `(source, published_at)`

**Metadata Examples:**
```json
{
  "feed": "https://feeds.reuters.com/reuters/businessNews",
  "source_quality_tier": 1,
  "matched_tickers": ["AAPL"],
  "ticker": "AAPL",           // From Finnhub/SEC
  "category": "general",       // From Finnhub
  "event_type_hint": "earnings_miss",  // Hints for classifier
  "summary_override": "..."    // SEC Item 2.02 extract
}
```

### B. Event Table (app/db/models.py, lines 42-59)

| Field | Type | Index | Notes |
|-------|------|-------|-------|
| id | Integer | PK | Auto-increment |
| event_type | String(128) | ✓ | e.g., "earnings_miss", "merger_acquisition" |
| entities | JSON | | List of mentioned entities |
| tickers | JSON | | List of affected tickers (e.g., ["AAPL"]) |
| severity | Integer | | 55–85 based on event_type |
| event_time | DateTime | ✓ | **When event occurred (not ingested)** |
| confidence | Integer | ✓ | 0-100 (validation score) |
| validation_status | String(32) | ✓ | "VALID", "WATCH", "REJECTED", or "PENDING" |
| conflict_reason | String(512) | | Reason if rejected |
| summary | Text | | Event summary |
| created_at | DateTime | ✓ | When we created the Event |
| signaled | Boolean | ✓ | True if agent generated signal |

**Composite Index:** `(event_time, confidence)`

### C. EventEvidence Table (app/db/models.py, lines 61-73)

| Field | Type | Notes |
|-------|------|-------|
| id | Integer | PK |
| event_id | Integer | FK → Event |
| raw_item_id | Integer | FK → RawItem |
| url | String(1024) | Link to source article |
| source | String(64) | Source name (e.g., "reuters") |
| source_tier | Integer | 0, 1, or 2 |
| captured_at | DateTime | When captured |
| summary | Text | Title/headline extract |

---

## 4. NEWS SENTIMENT AGENT (app/agents/news_sentiment.py)

### A. Agent Entry Point

**NewsSentimentAgent.analyze()**
- Location: lines 52-89
- Called with: `ticker`, `context` (optionally containing `as_of`)

### B. News Query (build_news_context_text, app/tools/news.py, lines 268-295)

**Default lookback: 336 hours (14 days)**

```python
def build_news_context_text(
    session: Session, ticker: str, lookback_hours: int = 336, as_of: datetime | None = None,
) -> str:
    # 1. Get recent events (app/tools/news.py, lines 114-172)
    events = get_recent_events(
        session, ticker=ticker, 
        lookback_hours=lookback_hours,  # 336h = 14 days
        limit=15,                        # Max 15 events
        as_of=as_of                      # For backtesting
    )
    
    # 2. Get ticker news (lines 207-265)
    news = get_ticker_news_summary(
        session, ticker=ticker,
        lookback_hours=lookback_hours,
        limit=10,                        # Max 10 news items
        as_of=as_of
    )
```

### C. get_recent_events() Query (lines 114-172)

**SQL Logic:**
```python
stmt = select(Event).where(
    Event.event_time >= since,  # since = ref_time - timedelta(hours=lookback_hours)
    Event.event_time <= ref_time,
)

# Ticker filter: JSON LIKE match for "TICKER" in tickers list
if ticker:
    stmt = stmt.where(Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%'))

# Min confidence filter
if row.confidence < min_confidence:
    continue

stmt.order_by(Event.event_time.desc()).limit(limit)
```

**Fields Returned:** 
- id, event_type, tickers, severity, confidence, event_time, validation_status, summary, evidence_count

**Key Filter: `as_of` parameter** (for backtesting)
- When set: Only events with `event_time <= as_of`
- Prevents future-look bias in agent analysis

### D. get_ticker_news_summary() Query (lines 207-265)

**Matching Strategy (3-tier):**

1. **Title word-boundary patterns** (Tier 1 results):
   - `(AAPL)%`, `% AAPL %`, `% AAPL,`, `% AAPL:%`, `% AAPL's`, `AAPL %`, `% AAPL`

2. **Company name patterns** (from _TICKER_COMPANY_NAMES, lines 13-111):
   - e.g., AAPL → ["Apple"], TSLA → ["Tesla"]
   - Pattern: `%Apple%` (case-insensitive)

3. **Metadata JSON lookup:**
   - Pattern: `%"ticker": "AAPL"%` (structured sources like SEC/Finnhub)

**SQL Query:**
```python
stmt = select(RawItem).where(
    RawItem.published_at >= since,
    sa.or_(
        *title_conditions,  # Multiple title patterns ORed
        RawItem.metadata_json.ilike(metadata_pattern),
    ),
)
if as_of:
    stmt = stmt.where(RawItem.published_at <= ref_time)

stmt.order_by(RawItem.source_tier.asc(), RawItem.published_at.desc()).limit(limit)
```

**Fields Returned:** 
- title, source, published_at, body_snippet (first 400 chars), source_tier

### E. LLM Prompt (NewsSentimentAgent, lines 12-44)

**System Prompt:**
```python
_SYSTEM_PROMPT = """\
Task: News sentiment analysis for equity trading.
You are an expert financial news analyst. Assess recent news and events for a
specific stock ticker and determine their near-term directional impact.
Be decisive — if news leans bullish or bearish, commit to a directional signal.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""
```

**User Prompt Template:**
```python
_USER_PROMPT_TEMPLATE = """\
Analyze the following recent news and events for ticker {ticker}, then determine the sentiment signal.

{news_context}

Return a JSON object with these exact fields:
{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "sentiment": "<STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH>",
  "event_strength": "<STRONG|MODERATE|WEAK|NOISE>",
  "key_catalyst": "<1 sentence describing the most impactful event, or 'none'>",
  "reasoning": "<2-3 sentence summary of news impact>"
}

Guidelines:
- BUY if recent news is bullish: earnings beat, positive guidance, deal announcement, upgrades, sector tailwinds, insider buying, buyback
- SHORT if recent news is bearish: earnings miss, negative guidance, fraud/legal, downgrades, sector headwinds, layoffs, revenue decline, tariff risk, competitive threat
- HOLD only if there is truly NO relevant news at all (zero articles). If any news exists, pick a direction!
- Even moderately positive/negative news should result in BUY/SHORT with moderate confidence (40-60)
- Multiple articles in the same direction → HIGH confidence (70+)
- confidence 60-100 = strong/clear signal, 30-60 = moderate signal, 0-30 = weak/absent
- Do NOT default to HOLD just because news is a few days old — news from the past 2 weeks is still actionable
- When in doubt between HOLD and a direction, CHOOSE THE DIRECTION with lower confidence
"""
```

---

## 5. NORMALIZATION SERVICE (app/normalization/service.py)

### A. Flow: build_clusters()

**Location:** lines 229-291

**Steps:**

1. **Load unprocessed RawItems:**
   ```python
   stmt = select(RawItem).where(RawItem.processed.is_(False))
   ```

2. **Extract Tickers** (lines 117-156):
   - Token-based symbol match: `"AAPL"` in text
   - Company name substring match: `"Apple"` → AAPL
   - Metadata hint: `metadata_json["ticker"]`
   - **Verification:** Only structured sources (SEC, earnings_release) or text mentions trusted

3. **Classify Event Type** (lines 195-201):
   - **Stage 1 - Routine Filing Detection** (lines 203-218):
     - Patterns: "filed 8-k", "form 10-q", etc.
     - Material markers: "restatement", "fraud", "guidance cut"
     - If routine + no material markers → event_type = "sec_filing"
   
   - **Stage 2 - Hint Check** (lines 243-254):
     - Use `metadata_json["event_type_hint"]` if available
   
   - **Stage 3 - Keyword Classify** (lines 165-171):
     - Fast keyword matching against EVENT_KEYWORDS
     - Returns "unknown" if no match
   
   - **Stage 4 - LLM Classify** (lines 173-193):
     - Only if: non-SEC, non-Finnhub items with no hint
     - Uses gemini-3-flash model
     - Prompt: 12 event types + "unknown"

4. **Clustering & Merging** (lines 256-289):
   - **Merge Window:** Configurable, default 0 (no merging)
   - When enabled: Groups events by `(primary_ticker, event_type, minute_bucket)`
   - **Cluster Key:**
     - If merge_window_min > 0: `(ticker, event_type, time_bucket)`
     - Else: `(item.id,)` – no merging
   - **Event time update:** Uses latest evidence time (to avoid future-looking)

### B. Severity Scoring (lines 220-227)

```python
SEVERE_EVENTS = {"financial_fraud", "audit_issue", "regulatory_penalty", "accident_disaster"}  # → 85
MID_EVENTS = {"earnings_miss", "guidance_cut", "major_litigation", "supply_chain_disruption"}  # → 70
DEFAULT = 55  # All others
```

### C. Output: NormalizedCluster

```python
@dataclass
class NormalizedCluster:
    canonical: CanonicalEvent  # Merged/normalized event
        - event_type: str
        - tickers: list[str]
        - severity: int (55–85)
        - event_time: datetime
        - summary: str
        - evidence_refs: list[int]  # Raw item IDs
    raw_items: list[RawItem]  # Supporting evidence
```

---

## 6. VALIDATION SERVICE (app/validation/service.py)

### A. Flow: validate_and_store()

**Location:** lines 179-236

**Scoring Algorithm** (lines 127-167):

```
Confidence = max(0, min(100, 
    source_score 
    + corroboration_score 
    + entity_consistency 
    - conflict_penalty
))

where:
  source_score = max(TIER_SCORE.get(t) for t in cluster_tiers)
    = 50 (Tier 0), 30 (Tier 1), 15 (Tier 2)
  
  corroboration_score = 0 if single_source else min(35, (source_count - 1) * 20)
    = 0 (1 source), 20 (2 sources), 35 (3+ sources)
  
  entity_consistency = 20 if len(tickers) == 1 else 12
  
  conflict_penalty = 40 if has_upgrade_and_downgrade_signals else 0
```

### B. Corroboration Window

**Default:** 180 minutes (config.validation_corroboration_window_minutes)

**Logic:**
- Within corroboration_window: Cluster matches prior event if:
  - Ticker overlap AND
  - Same event family OR summaries share 4+ common words (min 4 chars)
- When match: Aggregate sources & tiers

### C. Conflict Detection (lines 68-75)

```python
has_upgrade = any(token in text for token in 
    ("upgrade", "beats", "guides above", "wins case", "favorable ruling"))
has_downgrade = any(token in text for token in
    ("downgrade", "miss", "cuts", "fraud", "penalty", "lawsuit filed", "investigation"))

conflict = has_upgrade and has_downgrade
```

### D. Validation Status Assignment (lines 163-167)

```
VALID    if: has_tier0 OR source_count >= 2
WATCH    if: conflict detected OR single_source_only
REJECTED if: no_ticker_detected
```

---

## 7. QUALITY & FILTERING

### A. Deduplication (app/ingestion/service.py, lines 96-137)

**Location:** IngestionService.persist_items()

**Filters:**

1. **Recent Title Dedup** (lines 99-108):
   - Lookback: Last 6 hours
   - Normalize title (lowercase, punctuation removed)
   - Skip if normalized title in recent set

2. **URL/Hash Dedup** (lines 109-111):
   - Skip if `url` exists
   - Skip if `item_hash` exists
   - Uses SHA hash of (source, url, title)

**Result:** `IngestionResult` tracks:
- fetched: Total items pulled
- inserted: New items stored
- duplicate_dropped: Deduplicated
- raw_item_ids: IDs of inserted items

### B. Quality Scores in Validation (app/validation/service.py)

**Confidence Scoring Components:**

| Component | Points | Description |
|-----------|--------|-------------|
| Source Tier | 50/30/15 | Tier 0/1/2 respectively |
| Corroboration | 0–35 | +20 per additional source (up to 3) |
| Entity Count | 20/12 | Single vs. multi-ticker |
| Conflict Penalty | -40 | If upgrade AND downgrade signals |

**Total:** 0–100 range

### C. Event Type Filtering (Taxonomy, lines 7-42)

**Excluded from Trading:**
```python
EXCLUDED_FROM_TRADING = {
    "sec_filing",               # Routine filings (no signal)
    "unknown",                   # Unclassified (too noisy)
    "supply_chain_disruption",  # Too broad
    "guidance_cut",             # Keyword "warned" too loose
}
```

**Positive vs. Negative:**
```python
POSITIVE_EVENTS = {"buyback", "merger_acquisition"}
NEGATIVE_EVENTS = {
    "financial_fraud", "audit_issue", "earnings_miss", 
    "guidance_cut", "regulatory_penalty", "major_litigation",
    "layoff", "supply_chain_disruption", "accident_disaster",
    "policy_shock"
}
```

### D. Event Type Refinement (taxonomy.py, lines 109-140)

**Contextual Validation Patterns:**

1. **earnings_miss**: Rejects if positive earnings signals found
2. **regulatory_penalty**: Downgrades to litigation if no regulatory context
3. **major_litigation**: Rejects if positive resolution language found

---

## 8. RSS FEED CLIENT LOGIC (app/ingestion/rss_client.py)

### A. Ticker Relevance Scoring (lines 74-112)

**3-Tier Relevance System:**

```
Tier 1 (best):  Ticker or company name found in TITLE
Tier 2:         Found only in first 200 chars of BODY
Tier 3:         No SP100 match (general market news)
```

**Matching Strategy:**
- Token-based: Whole-word matches on SP100 tickers
- Company name: Longest-first to avoid shadowing
- Compiled regex for performance

**Result:** Each RSS item tagged with `(matched_tickers, source_tier)`

### B. RSS Feed Processing (lines 114-224)

**Per Feed:**
- Max 60 entries fetched
- Source name detected from URL host
- Published date: Uses `published` → `updated` → `pubDate` → `now()`
- Status: ONLINE if ≥1 item OR no bozo error

---

## 9. SEC FILING EXTRACTION (app/ingestion/sec_client.py)

### A. Forms Ingested (lines 24, 26)

**SUPPORTED_FORMS = {"8-K", "10-Q", "10-K", "6-K", "13D", "13G"}**

**FETCH_BODY_FORMS = {"8-K", "6-K"}** – Extract full body

### B. Event Detection

**Item 2.02 Extraction** (lines 36-40):
- Regex: `_ITEM_202_RE` matches "Item 2.02" + "results of operations and financial condition"
- Used for earnings signal detection

**Earnings Signals** (lines 30-35):
- Regex patterns: "quarterly results", "earnings release", "eps", "revenue", "guidance"

---

## 10. SUMMARY TABLE: Data Flow with Quality Checkpoints

| Stage | Component | Quality Control | Output |
|-------|-----------|-----------------|--------|
| **Fetch** | 5 Clients | Enable flags | RawItem list |
| **Persist** | IngestionService | 6h title + URL + hash dedup | RawItem (processed=False) |
| **Normalize** | NormalizationService | Keyword/LLM classify, routine filter | NormalizedCluster |
| **Validate** | ValidationService | Confidence score + corroboration | Event (status=VALID/WATCH/REJECTED) |
| **Query** | build_news_context_text | 14-day lookback + as_of filter | Text for LLM |
| **Sentiment** | NewsSentimentAgent | LLM analysis | Signal (BUY/SHORT/HOLD) |

---

## 11. KEY CONFIGURATION SETTINGS (app/core/config.py)

| Setting | Default | Purpose |
|---------|---------|---------|
| `enable_sec`, `enable_rss`, `enable_finnhub`, `enable_earnings_release_source` | True | Enable/disable sources |
| `rss_sources` | 7 feeds | RSS feed URLs |
| `normalization_merge_window_min` | 0 | Event merging window (disabled) |
| `validation_corroboration_window_minutes` | 180 | Event corroboration window |
| `validation_stale_minutes` | 120 | News age threshold for "STALE" |
| `event_tradeability_filter_enabled` | True | Quality filter enabled |
| `event_tradeability_min_score` | 55 | Min confidence to consider |
| `llm_classifier_model` | "gemini-3-flash" | Model for event classification |
| `sec_summary_model` | "gemini-3-flash" | Model for SEC extraction |
| `sec_poller_limit` | 100 | Max SEC filings per poll |

---

## 12. CURRENT LIMITATIONS & GAPS

1. **No real-time deduplication** – 6-hour window can miss duplicates across sources
2. **LLM classifier is optional** – Falls back to keyword only if LLM fails
3. **"unknown" events excluded from trading** – Conservative, but may miss signals
4. **Corroboration window (180min)** – Hard-coded; no dynamic adjustment
5. **Supply chain & guidance_cut excluded** – Patterns too broad/loose
6. **SEC earnings hint detection** – Relies on Item 2.02 + exhibit 99.1 patterns; may miss summary releases

