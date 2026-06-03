"""Job handler for crawl-puct queue jobs."""

import logging

from nodalpulse.crawlers.puct import PuctCrawler
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_puct(payload: dict) -> dict:
    result = await run_adapter(PuctCrawler(), "puct", payload.get("since"))
    logger.info("PUCT crawl complete: %s", result)
    return result
