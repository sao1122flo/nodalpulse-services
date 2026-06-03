"""Job handler for crawl-ercot queue jobs.

Runs both NPRR and Market Notices crawlers sequentially in one job.
Payload: {"since": "2026-05-01"}  (optional; defaults to last-crawled date per source)
"""

import logging

from nodalpulse.crawlers.ercot_mns import ErcotMarketNoticesCrawler
from nodalpulse.crawlers.ercot_nprr import ErcotNprrCrawler
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_ercot(payload: dict) -> dict:
    since = payload.get("since")
    logger.info("handle_crawl_ercot since=%s", since)

    nprr_result = await run_adapter(ErcotNprrCrawler(), "ercot-nprr", since)
    mn_result = await run_adapter(ErcotMarketNoticesCrawler(), "ercot-mn", since)

    result = {
        "nprr": nprr_result,
        "market_notices": mn_result,
        "total_saved": nprr_result["saved"] + mn_result["saved"],
    }
    logger.info("ERCOT crawl complete: %s", result)
    return result
