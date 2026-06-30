"""MD PSC crawl job handler.

Loads the persistent electric-case watch set (dockets jurisdiction='MD-PSC') and
hands it to the adapter, which unions it with cases discovered in the crawl window.
MD is a PJM state; dockets are tagged jurisdiction='MD-PSC' (per-state label, never
the bare market name) via run_adapter -> _SOURCE_JURISDICTION.
"""

import logging

from nodalpulse.crawlers.mdpsc import MdPscAdapter
from nodalpulse.db.filings import get_mdpsc_docket_set
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_mdpsc(payload: dict) -> dict:
    # max_filings (with an explicit since) bypasses run_adapter's 3-day lookback cap so a
    # backfill ({"since": "2025-01-01", "max_filings": N}) reaches the back-catalog.
    watch = await get_mdpsc_docket_set()
    result = await run_adapter(
        MdPscAdapter(watch),
        "mdpsc",
        payload.get("since"),
        max_filings=payload.get("max_filings"),
    )
    result["watched"] = len(watch)
    logger.info("MD PSC crawl complete: %s", result)
    return result
