"""Job handler for crawl-cpuc queue jobs."""

import logging

from nodalpulse.crawlers.cpuc import CpucAdapter
from nodalpulse.db.filings import get_cpuc_docket_set
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_cpuc(payload: dict) -> dict:
    """Crawl CPUC docs.cpuc.ca.gov for CPUC proceedings.

    Normal mode: crawls all proceedings in the watch set (jurisdiction='CPUC').
    On-demand mode: payload["proc_numbers"] limits the crawl to those proceedings only,
      bypassing the DB watch-set lookup. Used by /crawl/on-demand for user-triggered
      backfills of newly tracked proceedings.
    """
    if "proc_numbers" in payload:
        proc_set = set(payload["proc_numbers"])
        if not proc_set:
            return {"source": "cpuc", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}
        logger.info("handle_crawl_cpuc: on-demand %d proceedings: %s", len(proc_set), proc_set)
    else:
        proc_set = await get_cpuc_docket_set()
        if not proc_set:
            logger.warning(
                "handle_crawl_cpuc: no CPUC proceedings in watch set — "
                "seed via admin panel or wait for CAISO cross-ref extraction"
            )
            return {"source": "cpuc", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}
        logger.info("handle_crawl_cpuc: %d proceedings in watch set", len(proc_set))

    result = await run_adapter(
        CpucAdapter(proc_set), "cpuc", payload.get("since"),
        max_filings=payload.get("max_filings"),
    )
    result["watched"] = len(proc_set)
    logger.info("CPUC crawl complete: %s", result)
    return result
