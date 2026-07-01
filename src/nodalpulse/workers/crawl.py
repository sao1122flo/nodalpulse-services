"""Job handler for crawl-puct queue jobs."""

import logging

from nodalpulse.crawlers.puct import PuctCrawler
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_puct(payload: dict) -> dict:
    # On-demand backfill: payload["control_numbers"] targets specific dockets, skipping
    # L1 date-discovery so filings older than the daily window are reached. With an
    # explicit since + max_filings this bypasses run_adapter's short lookback cap.
    control_numbers = payload.get("control_numbers")
    cn_set = set(control_numbers) if control_numbers else None
    crawler = PuctCrawler(control_numbers=cn_set)
    result = await run_adapter(
        crawler,
        "puct",
        payload.get("since"),
        max_filings=payload.get("max_filings"),
        # Stamp last_crawled_at on the targeted dockets even if they yield 0 filings,
        # so a genuinely-empty tracked docket reads as "No filings yet" (not spinner).
        scope_docket_refs=cn_set,
    )
    if control_numbers and result.get("saved", 0) == 0:
        logger.warning(
            "PUCT on-demand crawl for %s saved 0 filings — coverage gap or invalid control numbers",
            control_numbers,
        )
    logger.info("PUCT crawl complete: %s", result)
    return result
