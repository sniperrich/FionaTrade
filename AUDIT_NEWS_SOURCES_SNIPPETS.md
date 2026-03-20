# FionaTrade News Data Sources - Key Code Snippets

---

## 1. RSS FEED LIST (app/core/config.py, lines 115-125)

```python
rss_sources: list[str] = Field(
    default_factory=lambda: [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.marketwatch.com/rss/topstories",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/topNews",
        "https://apnews.com/hub/financial-markets?format=rss",
        "https://seekingalpha.com/market_currents.xml",
    ]
)
```

---

## 2. NEWS TOOL QUERY (app/tools/news.py)

### A. get_recent_events() - Event Query (lines 114-172)

```python
def get_recent_events(
    session: Session,
    ticker: str | None = None,
    lookback_hours: int = 48,
    limit: int = 30,
    min_confidence: int = 0,
    as_of: datetime | None = None,
) -> list[dict]:
    """Return recent events, optionally filtered by ticker.
    
    as_of: Reference time for temporal filtering (backtest mode).
           When set, only events with event_time <= as_of are returned.
    """
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)

    if as_of:
        stmt = select(Event).where(
            Event.event_time >= since,
            Event.event_time <= ref_time,
        )
    else:
        stmt = select(Event).where(
            (Event.event_time >= since) | (Event.created_at >= since)
        )

    # SQL-level ticker filter using JSON text matching (works for SQLite + Postgres)
    if ticker:
        stmt = stmt.where(Event.tickers.cast(sa.Text).ilike(f'%"{ticker.upper()}"%'))

    stmt = stmt.order_by(Event.event_time.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()

    results = []
    for row in rows:
        if row.confidence < min_confidence:
            continue

        evidence_count = session.execute(
            select(EventEvidence).where(EventEvidence.event_id == row.id)
        ).scalars().all()

        results.append({
            "id": row.id,
            "event_type": row.event_type,
            "tickers": row.tickers,
            "severity": row.severity,
            "confidence": row.confidence,
            "event_time": row.event_time.isoformat(),
            "validation_status": row.validation_status,
            "summary": row.summary,
            "evidence_count": len(evidence_count),
        })

    return results
```

### B. get_ticker_news_summary() - Raw Item Query (lines 207-265)

```python
def get_ticker_news_summary(
    session: Session, ticker: str, lookback_hours: int = 72, limit: int = 15,
    as_of: datetime | None = None,
) -> list[dict]:
    """Return recent RawItems mentioning a ticker directly."""
    ref_time = as_of or datetime.now(timezone.utc)
    since = ref_time - timedelta(hours=lookback_hours)
    ticker_upper = ticker.upper()

    # Word-boundary patterns for ticker symbol
    title_patterns = [
        f"({ticker_upper})%",   # (AAPL)...
        f"% {ticker_upper} %",  # ... AAPL ...
        f"% {ticker_upper},%",  # ... AAPL,...
        f"% {ticker_upper}:%",  # ... AAPL:...
        f"% {ticker_upper}'%",  # ... AAPL's...
        f"{ticker_upper} %",    # AAPL ... (start)
        f"% {ticker_upper}",    # ... AAPL (end)
    ]

    # Company name patterns (catches "Apple", "Amazon", etc.)
    company_names = _TICKER_COMPANY_NAMES.get(ticker_upper, [])
    for name in company_names:
        title_patterns.append(f"%{name}%")

    # Metadata exact match for SEC/Finnhub tagged items
    metadata_pattern = f'%"ticker": "{ticker_upper}"%'

    title_conditions = [RawItem.title.ilike(p) for p in title_patterns]

    stmt = select(RawItem).where(
        RawItem.published_at >= since,
        sa.or_(
            *title_conditions,
            RawItem.metadata_json.ilike(metadata_pattern),
        ),
    )
    if as_of:
        stmt = stmt.where(RawItem.published_at <= ref_time)
    
    rows = session.execute(
        stmt.order_by(RawItem.source_tier.asc(), RawItem.published_at.desc()).limit(limit)
    ).scalars().all()

    results = []
    for row in rows:
        results.append({
            "title": row.title,
            "source": row.source,
            "published_at": row.published_at.isoformat(),
            "body_snippet": row.body[:400],
            "source_tier": row.source_tier,
        })

    return results
```

### C. build_news_context_text() - LLM Input Builder (lines 268-295)

```python
def build_news_context_text(
    session: Session, ticker: str, lookback_hours: int = 336, as_of: datetime | None = None,
) -> str:
    """Build a compact text block of recent news/events for LLM prompts.
    
    Default 336h (14 days) lookback to ensure adequate news coverage.
    """
    events = get_recent_events(session, ticker=ticker, lookback_hours=lookback_hours, limit=15, as_of=as_of)
    news = get_ticker_news_summary(session, ticker=ticker, lookback_hours=lookback_hours, limit=10, as_of=as_of)

    lines: list[str] = [f"=== RECENT EVENTS FOR {ticker} ==="]
    if events:
        for ev in events:
            lines.append(
                f"  [{ev['event_time'][:16]}] {ev['event_type']} "
                f"(conf={ev['confidence']}, sev={ev['severity']}): {ev['summary'][:150]}"
            )
    else:
        lines.append("  No recent events found.")

    lines.append(f"\n=== RECENT NEWS FOR {ticker} ===")
    if news:
        for item in news:
            lines.append(f"  [{item['source']}] {item['title']}")
    else:
        lines.append("  No recent news found.")

    return "\n".join(lines)
```

**Key Features:**
- Default 336-hour (14-day) lookback for max news coverage
- **as_of parameter**: When set, only includes events/news with timestamps <= as_of (critical for backtesting)
- Events sorted by recency; rated by confidence & severity
- News sorted by source tier (Tier 0 first) then recency
- Title patterns: Word-boundary matches for tickers + company names
- Metadata lookup for structured sources (SEC, Finnhub)

---

## 3. NEWS SENTIMENT AGENT PROMPT (app/agents/news_sentiment.py)

### System Prompt (lines 12-18)

```python
_SYSTEM_PROMPT = """\
Task: News sentiment analysis for equity trading.
You are an expert financial news analyst. Assess recent news and events for a
specific stock ticker and determine their near-term directional impact.
Be decisive — if news leans bullish or bearish, commit to a directional signal.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""
```

### User Prompt Template (lines 20-44)

```python
_USER_PROMPT_TEMPLATE = """\
Analyze the following recent news and events for ticker {ticker}, then determine the sentiment signal.

{news_context}

Return a JSON object with these exact fields:
{{
  "signal": "<BUY|SHORT|HOLD>",
  "confidence": <integer 0-100>,
  "sentiment": "<STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH>",
  "event_strength": "<STRONG|MODERATE|WEAK|NOISE>",
  "key_catalyst": "<1 sentence describing the most impactful event, or 'none'>",
  "reasoning": "<2-3 sentence summary of news impact>"
}}

Guidelines:
- BUY if recent news is bullish: earnings beat, positive guidance, deal announcement, 
  upgrades, sector tailwinds, insider buying, buyback
- SHORT if recent news is bearish: earnings miss, negative guidance, fraud/legal, 
  downgrades, sector headwinds, layoffs, revenue decline, tariff risk, competitive threat
- HOLD only if there is truly NO relevant news at all (zero articles). 
  If any news exists, pick a direction!
- Even moderately positive/negative news should result in BUY/SHORT with moderate confidence (40-60)
- Multiple articles in the same direction → HIGH confidence (70+)
- confidence 60-100 = strong/clear signal, 30-60 = moderate signal, 0-30 = weak/absent
- Do NOT default to HOLD just because news is a few days old — 
  news from the past 2 weeks is still actionable
- When in doubt between HOLD and a direction, CHOOSE THE DIRECTION with lower confidence
"""
```

### Agent Usage (lines 52-89)

```python
def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
    try:
        as_of = (context or {}).get("as_of")
        # Default 336-hour (14-day) lookback
        news_text = build_news_context_text(session, ticker, lookback_hours=336, as_of=as_of)
        market_ctx = build_market_context_text(session, ticker, as_of=as_of)
        combined = f"{news_text}\n\n{market_ctx}"

        # Call LLM (OpenAI-compatible model)
        raw = self._call_llm(_SYSTEM_PROMPT, user_prompt, response_format="json")
        parsed = self._parse_json_response(raw)

        if not parsed:
            return AgentSignal.no_signal(self.name, "LLM unavailable or unparseable response")

        signal = parsed.get("signal", "HOLD").upper()
        if signal not in ("BUY", "SHORT", "HOLD"):
            signal = "HOLD"

        return AgentSignal(
            agent_name=self.name,
            signal=signal,
            confidence=int(parsed.get("confidence", 30)),
            reasoning=parsed.get("reasoning", ""),
            metadata={
                "sentiment": parsed.get("sentiment", "NEUTRAL"),
                "event_strength": parsed.get("event_strength", "WEAK"),
                "key_catalyst": parsed.get("key_catalyst", "none"),
            },
        )

    except Exception as exc:
        logger.exception("[news_sentiment] Unexpected error for %s: %s", ticker, exc)
        return AgentSignal.error_signal(self.name, str(exc))
```

---

## 4. QUALITY FILTERS & SCORING

### A. Deduplication (app/ingestion/service.py, lines 96-137)

```python
def persist_items(self, session: Session, fetched_items: list[RawNewsItem], checks: list[SourceCheck]) -> IngestionResult:
    self._persist_source_checks(session, checks)

    recent_title_set = self._recent_titles(session)  # 6-hour recency window
    inserted = 0
    duplicates = 0
    raw_ids: list[int] = []

    for item in fetched_items:
        normalized_title = normalize_title(item.title)
        
        # Filter 1: Recent title dedup (6 hours, normalized)
        if normalized_title in recent_title_set:
            duplicates += 1
            continue
        
        # Filter 2: URL + hash dedup
        if self._exists(session, item):  # Checks url == OR item_hash ==
            duplicates += 1
            continue

        # Insert new item
        row = RawItem(
            source=item.source,
            source_tier=item.source_tier,
            url=item.url,
            title=item.title,
            body=item.body,
            published_at=ensure_utc(item.published_at),
            ingested_at=ensure_utc(item.ingested_at),
            item_hash=item.hash,
            metadata_json={**item.metadata, "normalized_title": normalized_title},
            processed=False,
        )
        session.add(row)
        session.flush()

        inserted += 1
        raw_ids.append(row.id)
        recent_title_set.add(normalized_title)

    return IngestionResult(
        fetched=len(fetched_items),
        inserted=inserted,
        duplicate_dropped=duplicates,
        raw_item_ids=raw_ids,
    )
```

### B. Validation Confidence Scoring (app/validation/service.py, lines 127-167)

```python
def _score(
    self,
    cluster: NormalizedCluster,
    recent_history: list[_RecentEventSnapshot],
) -> tuple[int, str, str | None]:
    if not cluster.canonical.tickers:
        return 0, "REJECTED", "no_ticker_detected"

    event_time = ensure_utc(cluster.canonical.event_time)
    summary = cluster.canonical.summary or self._current_text(cluster)
    sources = {str(item.source or "").lower() for item in cluster.raw_items if item.source}
    tiers = [self._normalize_source_tier(item.source_tier) for item in cluster.raw_items]
    conflict_texts = [summary]

    # Check for corroborating events within 180-minute window
    for snapshot in recent_history:
        snap_time = ensure_utc(snapshot.event_time)
        if snap_time > event_time:
            continue
        if event_time - snap_time > self.corroboration_window:
            continue
        if not self._corroborates(cluster, snapshot):
            continue
        sources.update(snapshot.sources)  # Aggregate sources
        tiers.extend(snapshot.tiers)
        conflict_texts.append(snapshot.summary)

    # Score components
    source_count = len(sources)
    has_tier0 = any(t == 0 for t in tiers)
    conflict = self._has_conflict(conflict_texts)

    source_score = max(TIER_SCORE.get(t, 10) for t in tiers)      # 50/30/15 for Tier0/1/2
    corroboration_score = 0 if source_count <= 1 else min(35, (source_count - 1) * 20)
    entity_consistency = 20 if len(cluster.canonical.tickers) == 1 else 12
    conflict_penalty = 40 if conflict else 0

    confidence = max(0, min(100, int(source_score + corroboration_score + entity_consistency - conflict_penalty)))
    
    if conflict:
        return confidence, "WATCH", "source_conflict_detected"
    if has_tier0 or source_count >= 2:
        return confidence, "VALID", None
    return confidence, "WATCH", "single_source_only"
```

### C. Source Tiers & Scores (app/analysis/taxonomy.py, lines 44-64)

```python
SOURCE_TIER = {
    "sec": 0,                   # Authoritative
    "exchange": 0,
    "company": 0,
    "earnings_release": 0,
    "reuters": 1,               # Major news outlets
    "bloomberg": 1,
    "wsj": 1,
    "ft": 1,
    "cnbc": 1,
    "marketwatch": 2,           # Secondary
    "seekingalpha": 2,
    "finnhub": 2,
    "rss": 2,
}

TIER_SCORE = {
    0: 50,  # Tier 0 → 50 points
    1: 30,  # Tier 1 → 30 points
    2: 15,  # Tier 2 → 15 points
}
```

---

## 5. NORMALIZATION FLOW (app/normalization/service.py)

### Event Type Classification (lines 195-201)

```python
def _infer_event_type(self, text: str) -> str:
    # Stage 1: Keyword pass first (fast, free)
    result = self._keyword_classify(text)
    if result != "unknown":
        return result
    
    # Stage 2: Fall back to LLM classifier for ambiguous items
    return resolve_event_type_for_text(self._llm_classify(text), text)

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
            model=self.settings.llm_classifier_model,  # "gemini-3-flash"
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
```

### Cluster Build (lines 229-291)

```python
def build_clusters(self, session: Session, raw_ids: Iterable[int] | None = None) -> list[NormalizedCluster]:
    stmt = select(RawItem).where(RawItem.processed.is_(False))
    if raw_ids:
        stmt = stmt.where(RawItem.id.in_(list(raw_ids)))
    rows = session.execute(stmt.order_by(RawItem.published_at.asc())).scalars().all()
    
    merge_window_min = max(0, int(getattr(self.settings, "normalization_merge_window_min", 0)))

    grouped: dict[tuple[object, ...], NormalizedCluster] = {}
    for item in rows:
        text = f"{item.title} {item.body}"
        tickers = self._extract_tickers(text, item.metadata_json, source=item.source)
        
        # Classify event type
        if self._is_routine_filing_item(item, text):
            event_type = "sec_filing"
        else:
            hinted_event_type = str((item.metadata_json or {}).get("event_type_hint") or "").strip().lower()
            if hinted_event_type in _VALID_EVENT_TYPES:
                event_type = resolve_event_type_for_text(hinted_event_type, text)
            else:
                has_ticker_meta = bool((item.metadata_json or {}).get("ticker"))
                if has_ticker_meta:
                    event_type = self._keyword_classify(text)  # Skip LLM for Finnhub
                else:
                    event_type = self._infer_event_type(text)  # Use LLM
        
        primary_ticker = tickers[0] if tickers else "UNKNOWN"
        
        # Cluster key (merge window disabled by default)
        if merge_window_min > 0:
            bucket = minute_bucket(item.published_at, width_min=merge_window_min).isoformat()
            key = (primary_ticker, event_type, bucket)
        else:
            key = (item.id,)  # No merging

        if key not in grouped:
            grouped[key] = NormalizedCluster(
                canonical=CanonicalEvent(
                    event_type=event_type,
                    entities=tickers,
                    tickers=tickers,
                    severity=self._severity(event_type),
                    event_time=ensure_utc(item.published_at),
                    evidence_refs=[item.id],
                    summary=self._item_summary(item),
                ),
                raw_items=[item],
            )
        else:
            # Merge: add to existing cluster
            group = grouped[key]
            group.raw_items.append(item)
            group.canonical.evidence_refs.append(item.id)
            for t in tickers:
                if t not in group.canonical.tickers:
                    group.canonical.tickers.append(t)
                    group.canonical.entities.append(t)
            # Update event time to latest evidence
            item_ts = ensure_utc(item.published_at)
            if item_ts > ensure_utc(group.canonical.event_time):
                group.canonical.event_time = item_ts
                group.canonical.summary = self._item_summary(item)

    return list(grouped.values())
```

---

## 6. EVENT KEYWORDS (app/analysis/taxonomy.py, lines 7-22)

```python
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
```

---

## 7. RawItem & Event MODELS (app/db/models.py)

### RawItem (lines 26-40)

```python
class RawItem(Base):
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_tier: Mapped[int] = mapped_column(Integer, default=2)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    item_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
```

### Event (lines 42-59)

```python
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    tickers: Mapped[list] = mapped_column(JSON, default=list, index=False)
    severity: Mapped[int] = mapped_column(Integer, default=50)
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0, index=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    conflict_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    signaled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    evidence: Mapped[list["EventEvidence"]] = relationship("EventEvidence", back_populates="event")
```

