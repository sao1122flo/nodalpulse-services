"""Worker process — polls the job queue and dispatches handlers."""

import asyncio
import logging

from nodalpulse.queue.pg_queue import run_worker
from nodalpulse.workers.compose_brief import handle_compose_brief
from nodalpulse.workers.crawl import handle_crawl_puct
from nodalpulse.workers.crawl_ercot import handle_crawl_ercot
from nodalpulse.workers.extract import handle_extract

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=__import__("sys").stdout)
logger = logging.getLogger(__name__)

HANDLERS = {
    "crawl-puct": handle_crawl_puct,
    "crawl-ercot": handle_crawl_ercot,
    "extract": handle_extract,
    "compose-brief": handle_compose_brief,
}


async def main() -> None:
    logger.info("Worker starting — handlers: %s", list(HANDLERS))
    await asyncio.gather(*[run_worker(kind, handler) for kind, handler in HANDLERS.items()])


if __name__ == "__main__":
    asyncio.run(main())
