#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis.service import AnalysisService
from app.core.config import get_settings
from app.db.database import db_session, init_db
from app.db.models import Event, EventEvidence, RawItem
from app.ingestion.service import IngestionService
from app.normalization.service import NormalizationService
from app.validation.service import ValidationService


def _latest_event_for_ticker(session, ticker: str) -> Event | None:
    rows = (
        session.execute(
            select(Event)
            .where(Event.event_type == "sec_earnings_release")
            .order_by(Event.event_time.desc(), Event.id.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    ticker = ticker.upper().strip()
    for event in rows:
        if ticker in {str(x).upper() for x in (event.tickers or [])}:
            return event
    return None


def _evidence_payload(session, event: Event) -> list[dict]:
    rows = session.execute(
        select(EventEvidence, RawItem)
        .join(RawItem, RawItem.id == EventEvidence.raw_item_id)
        .where(EventEvidence.event_id == event.id)
        .order_by(EventEvidence.id.asc())
    ).all()

    payload = []
    for evidence, raw in rows:
        payload.append(
            {
                "source": evidence.source,
                "source_tier": evidence.source_tier,
                "url": evidence.url,
                "published_at": raw.published_at.isoformat() if raw and raw.published_at else None,
                "title": raw.title if raw else evidence.summary,
                "body_preview": ((raw.body if raw else evidence.summary) or "")[:1200],
                "metadata": raw.metadata_json if raw else {},
            }
        )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a minimal AAPL-style SEC -> event -> LLM signal loop without touching the full universe."
    )
    parser.add_argument("--ticker", default="AAPL", help="Ticker to fetch, default AAPL")
    parser.add_argument("--max-forms", type=int, default=20, help="Max recent SEC forms to inspect for the ticker")
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="If latest SEC item is already in DB, reuse the latest existing sec_earnings_release event",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper().strip()
    init_db()
    base_settings = get_settings()
    settings = base_settings.model_copy(update={"sp100_tickers": [ticker]})

    ingestion = IngestionService(settings)
    normalization = NormalizationService(settings)
    validation = ValidationService(settings.validation_corroboration_window_minutes)
    analysis = AnalysisService(settings)

    started = time.perf_counter()
    timings: dict[str, float] = {}

    with db_session() as session:
        t0 = time.perf_counter()
        sec_items, sec_check = ingestion.sec.fetch(
            session,
            tickers=[ticker],
            max_forms_per_ticker=args.max_forms,
            stop_after_first_earnings=True,
        )
        timings["fetch_sec_seconds"] = round(time.perf_counter() - t0, 3)

        sec_items = [item for item in sec_items if (item.metadata or {}).get("event_type_hint") == "sec_earnings_release"]

        t0 = time.perf_counter()
        ingested = ingestion.persist_items(session, sec_items, [sec_check])
        timings["persist_seconds"] = round(time.perf_counter() - t0, 3)

        latest_event = None
        validation_payload = {
            "created_events": 0,
            "valid_events": 0,
            "watch_events": 0,
            "rejected_events": 0,
            "event_ids": [],
        }

        if ingested.raw_item_ids:
            t0 = time.perf_counter()
            clusters = normalization.build_clusters(session, raw_ids=ingested.raw_item_ids)
            validated = validation.validate_and_store(session, clusters)
            session.flush()
            timings["normalize_validate_seconds"] = round(time.perf_counter() - t0, 3)
            validation_payload = {
                "created_events": validated.created_events,
                "valid_events": validated.valid_events,
                "watch_events": validated.watch_events,
                "rejected_events": validated.rejected_events,
                "event_ids": validated.event_ids,
            }
            if validated.event_ids:
                latest_event = session.execute(
                    select(Event).where(Event.id == validated.event_ids[-1])
                ).scalar_one_or_none()
        else:
            timings["normalize_validate_seconds"] = 0.0

        reused_existing_event = False
        if latest_event is None and args.allow_existing:
            latest_event = _latest_event_for_ticker(session, ticker)
            reused_existing_event = latest_event is not None

        tradeability = None
        signal_payload = None
        evidence = []
        if latest_event is not None:
            t0 = time.perf_counter()
            tradeability = analysis.assess_tradeability(latest_event, session=session)
            signal = analysis.event_to_signal(latest_event, session=session)
            timings["analysis_seconds"] = round(time.perf_counter() - t0, 3)
            evidence = _evidence_payload(session, latest_event)
            if signal is not None:
                signal_payload = {
                    "action": signal.action,
                    "ticker": signal.ticker,
                    "confidence": signal.confidence,
                    "horizon_min": signal.horizon_min,
                    "horizon_profile": signal.horizon_profile,
                    "position_pct_suggestion": signal.position_pct_suggestion,
                    "reason": signal.reason,
                    "expires_at": signal.expires_at.isoformat(),
                    "fallback_used": signal.fallback_used,
                }
        else:
            timings["analysis_seconds"] = 0.0

        payload = {
            "ticker": ticker,
            "max_forms": args.max_forms,
            "timings": {
                **timings,
                "total_seconds": round(time.perf_counter() - started, 3),
            },
            "source_check": {
                "status": sec_check.status,
                "error_message": sec_check.error_message,
                "details": sec_check.details,
            },
            "ingestion": {
                "fetched_items": len(sec_items),
                "inserted": ingested.inserted,
                "duplicate_dropped": ingested.duplicate_dropped,
                "raw_item_ids": ingested.raw_item_ids,
            },
            "validation": validation_payload,
            "reused_existing_event": reused_existing_event,
            "event": None
            if latest_event is None
            else {
                "id": latest_event.id,
                "event_type": latest_event.event_type,
                "tickers": latest_event.tickers,
                "event_time": latest_event.event_time.isoformat() if latest_event.event_time else None,
                "validation_status": latest_event.validation_status,
                "confidence": latest_event.confidence,
                "severity": latest_event.severity,
                "summary": latest_event.summary,
            },
            "tradeability": tradeability,
            "signal": signal_payload,
            "evidence": evidence[:2],
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
