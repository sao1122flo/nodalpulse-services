"""NJ BPU crawl job handler.

Crawls the New Jersey Board of Public Utilities public-document portal
(publicaccess.bpu.state.nj.us) for electric-sector regulatory filings. NJ is a
PJM state; dockets are tagged jurisdiction='NJ-BPU' (per-state label — never the
bare market name) via run_adapter -> _SOURCE_JURISDICTION.
"""

import logging

from nodalpulse.crawlers.njbpu import NjBpuAdapter
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_njbpu(payload: dict) -> dict:
    # Forward max_filings so a backfill ({"since": "2025-01-01", "max_filings": N})
    # bypasses run_adapter's 3-day rolling-lookback cap and ingests the back-catalog.
    # Daily cron passes no max_filings → normal incremental (3-day window).
    result = await run_adapter(
        NjBpuAdapter(),
        "njbpu",
        payload.get("since"),
        max_filings=payload.get("max_filings"),
    )
    logger.info("NJ BPU crawl complete: %s", result)
    return result
