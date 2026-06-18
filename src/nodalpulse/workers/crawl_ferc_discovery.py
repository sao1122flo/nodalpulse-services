"""Job handler for crawl-ferc-discovery queue jobs (issue #85).

Broad FERC metadata sweep: no docket constraint, no extraction, no LLM, no R2.
Populates discovery_feed for the entity-match engine in compose_brief.

Environment variables:
  DISCOVERY_MAX_FILINGS  — cap per sweep (default: 500)
  WORKER_MAX_LOOKBACK_DAYS — reused for since_date floor (default: 3)
"""

import logging
import os
from datetime import date, timedelta

from nodalpulse.crawlers.ferc_discovery import fetch_ferc_discovery
from nodalpulse.db.discovery import cleanup_expired_discovery, upsert_discovery_items

logger = logging.getLogger(__name__)

_DISCOVERY_MAX_FILINGS = int(os.environ.get("DISCOVERY_MAX_FILINGS", "500"))
_MAX_LOOKBACK_DAYS = int(os.environ.get("WORKER_MAX_LOOKBACK_DAYS", "3"))


async def handle_crawl_ferc_discovery(payload: dict) -> dict:
    """Sweep recent FERC Electric-library filings and populate discovery_feed.

    since: ISO date string; defaults to yesterday. Floored at MAX_LOOKBACK_DAYS
    to prevent accidental deep backfills from stale payloads.
    """
    raw_since = payload.get("since") or (date.today() - timedelta(days=1)).isoformat()
    floor = (date.today() - timedelta(days=_MAX_LOOKBACK_DAYS)).isoformat()
    since = max(raw_since, floor)
    since_date = date.fromisoformat(since)

    logger.info(
        "handle_crawl_ferc_discovery: since=%s max_filings=%d", since, _DISCOVERY_MAX_FILINGS
    )

    items = await fetch_ferc_discovery(since_date=since_date, max_filings=_DISCOVERY_MAX_FILINGS)
    saved, skipped = await upsert_discovery_items(items)
    cleaned = await cleanup_expired_discovery()

    result = {
        "fetched": len(items),
        "saved": saved,
        "skipped": skipped,
        "cleaned": cleaned,
        "since": since,
    }
    logger.info("crawl-ferc-discovery complete: %s", result)
    return result
