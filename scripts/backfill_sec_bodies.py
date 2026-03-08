"""
Backfill SEC filing body text for raw_items that only have the placeholder.
Fetches actual HTML content from SEC EDGAR for 8-K/6-K/10-K/10-Q forms.
"""
from __future__ import annotations

import argparse
import logging
import re
from time import sleep

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import db_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FETCH_FORMS = {"8-K", "6-K", "10-K", "10-Q"}
MAX_CHARS = 8000


def _fetch_text(client: httpx.Client, url: str, headers: dict) -> str:
    try:
        resp = client.get(url, headers={**headers, "Accept": "text/html,application/xhtml+xml"}, timeout=20.0)
        if resp.status_code != 200:
            return ""
        html = resp.text
    except Exception:
        return ""

    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill SEC filing body text")
    parser.add_argument("--forms", default="8-K,6-K,10-K,10-Q", help="Comma-separated forms to backfill")
    parser.add_argument("--limit", type=int, default=0, help="Max items to process (0=all)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Sleep seconds between requests")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()

    target_forms = {f.strip().upper() for f in args.forms.split(",")}
    settings = get_settings()
    headers = {
        "User-Agent": settings.sec_user_agent,
        "Accept": "application/json",
    }

    with db_session() as session:
        rows = session.execute(text("""
            SELECT id, url, body, metadata_json
            FROM raw_items
            WHERE source = 'sec'
            AND (body IS NULL OR body = '' OR body LIKE 'SEC filing%')
            ORDER BY id DESC
        """)).fetchall()

    logger.info("Found %d SEC items with placeholder body", len(rows))

    eligible = []
    for row in rows:
        import json
        meta = row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
        form = meta.get("form", "").upper()
        if form in target_forms:
            eligible.append((row[0], row[1], form))

    if args.limit > 0:
        eligible = eligible[: args.limit]

    logger.info("Eligible items to fetch: %d (forms: %s)", len(eligible), target_forms)

    updated = 0
    failed = 0

    with httpx.Client(timeout=20.0, headers=headers) as client:
        for item_id, url, form in eligible:
            body = _fetch_text(client, url, headers)
            if body and not body.startswith("EDGAR"):
                word_count = len(body.split())
                if word_count < 20:
                    logger.warning("id=%d form=%s fetched only %d words, skipping", item_id, form, word_count)
                    failed += 1
                    sleep(args.sleep)
                    continue

                if not args.dry_run:
                    with db_session() as session:
                        session.execute(
                            text("UPDATE raw_items SET body = :body, processed = 0 WHERE id = :id"),
                            {"body": body, "id": item_id},
                        )
                logger.info("id=%d form=%s url=...%s chars=%d", item_id, form, url[-40:], len(body))
                updated += 1
            else:
                logger.warning("id=%d form=%s fetch failed or empty: %s", item_id, form, url[-60:])
                failed += 1

            sleep(args.sleep)

    logger.info("Done. updated=%d failed=%d dry_run=%s", updated, failed, args.dry_run)


if __name__ == "__main__":
    main()
