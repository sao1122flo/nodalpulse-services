"""Job handler for crawl-cpuc queue jobs."""

import logging

from nodalpulse.crawlers.cpuc import CpucAdapter
from nodalpulse.db.filings import get_cpuc_docket_set
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_cpuc(payload: dict) -> dict:
    """Crawl CPUC docs.cpuc.ca.gov for all proceedings currently in the CPUC watch set.

    Watch set = all dockets with jurisdiction='CPUC' in the dockets table, normalized
    to the search-form format (A.25-08-008 → A2508008). Seeds include:
    - CPUC proceeding cross-refs extracted from CAISO filings (#53)
    - Manually seeded dockets via admin panel
    """
    proc_set = await get_cpuc_docket_set()
    if not proc_set:
        logger.warning(
            "handle_crawl_cpuc: no CPUC proceedings in watch set — "
            "seed via admin panel or wait for CAISO cross-ref extraction"
        )
        return {"source": "cpuc", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}

    logger.info("handle_crawl_cpuc: %d proceedings in watch set", len(proc_set))
    result = await run_adapter(CpucAdapter(proc_set), "cpuc", payload.get("since"))
    result["watched"] = len(proc_set)
    logger.info("CPUC crawl complete: %s", result)
    return result
