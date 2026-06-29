"""IMM crawl job handler.

Crawls the PJM Independent Market Monitor (Monitoring Analytics) filings site.
IMM files at FERC — docket IDs parsed from filenames create PJM-FERC docket rows
via run_adapter → find_or_create_docket, which the FercAdapter then watches.
"""

import logging

from nodalpulse.crawlers.imm import ImmAdapter
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_imm(payload: dict) -> dict:
    # Forward max_filings so a backfill ({"since": "2025-01-01", "max_filings": N})
    # bypasses run_adapter's 3-day rolling-lookback cap and ingests the back-catalog.
    # Daily cron passes no max_filings → normal incremental (3-day window).
    result = await run_adapter(
        ImmAdapter(),
        "imm",
        payload.get("since"),
        max_filings=payload.get("max_filings"),
    )
    logger.info("IMM crawl complete: %s", result)
    return result
