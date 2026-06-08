"""Worker process — polls the job queue and dispatches handlers."""

import asyncio
import logging

from nodalpulse.queue.pg_queue import run_worker
from nodalpulse.workers.brief_history_export import handle_brief_history_export
from nodalpulse.workers.compose_brief import handle_compose_brief
from nodalpulse.workers.crawl import handle_crawl_puct
from nodalpulse.workers.crawl_ercot import handle_crawl_ercot
from nodalpulse.workers.crawl_caiso import handle_crawl_caiso
from nodalpulse.workers.crawl_cpuc import handle_crawl_cpuc
from nodalpulse.workers.crawl_ferc import handle_crawl_ferc
from nodalpulse.workers.crawl_pjm import handle_crawl_pjm
from nodalpulse.workers.crawl_imm import handle_crawl_imm
from nodalpulse.workers.crawl_pjm_calendar import handle_crawl_pjm_calendar
from nodalpulse.workers.extract import handle_extract
from nodalpulse.workers.refresh_extraction import handle_refresh_extraction

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=__import__("sys").stdout)
logger = logging.getLogger(__name__)

HANDLERS = {
    "crawl-puct":            handle_crawl_puct,
    "crawl-ercot":           handle_crawl_ercot,
    "crawl-caiso":           handle_crawl_caiso,
    "crawl-cpuc":            handle_crawl_cpuc,
    "crawl-ferc":            handle_crawl_ferc,
    "crawl-pjm":             handle_crawl_pjm,
    "crawl-imm":             handle_crawl_imm,
    "crawl-pjm-calendar":   handle_crawl_pjm_calendar,
    "extract":               handle_extract,
    "refresh-extraction":    handle_refresh_extraction,
    "compose-brief":         handle_compose_brief,
    "brief-history-export":  handle_brief_history_export,
}


async def main() -> None:
    logger.info("Worker starting — handlers: %s", list(HANDLERS))
    await asyncio.gather(*[run_worker(kind, handler) for kind, handler in HANDLERS.items()])


if __name__ == "__main__":
    asyncio.run(main())
