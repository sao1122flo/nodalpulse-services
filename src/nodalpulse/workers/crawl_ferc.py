"""Job handler for crawl-ferc queue jobs."""

import logging

from nodalpulse.crawlers.ferc import FercAdapter
from nodalpulse.db.filings import get_ferc_docket_set
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_ferc(payload: dict) -> dict:
    """Crawl FERC eCollection RSS for all dockets currently tracked in the dockets table.

    If no FERC dockets exist yet, logs a warning and returns a zero-count result.
    Seed dockets via the admin panel or services_schema.sql before enabling this handler.
    """
    docket_set = await get_ferc_docket_set()
    if not docket_set:
        logger.warning("handle_crawl_ferc: no FERC dockets tracked -- seed the dockets table first")
        return {"source": "ferc", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}

    logger.info("handle_crawl_ferc: %d dockets in watch set", len(docket_set))
    result = await run_adapter(FercAdapter(docket_set), "ferc", payload.get("since"))
    result["watched"] = len(docket_set)
    logger.info("FERC crawl complete: %s", result)
    return result
