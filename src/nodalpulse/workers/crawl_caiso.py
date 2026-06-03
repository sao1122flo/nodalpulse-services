"""CAISO crawl job handler."""

import logging

from nodalpulse.crawlers.caiso import CaisoAdapter
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_caiso(payload: dict) -> dict:
    result = await run_adapter(CaisoAdapter(), "caiso", payload.get("since"))
    logger.info("CAISO crawl complete: %s", result)
    return result
