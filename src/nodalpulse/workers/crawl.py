"""Job handler for crawl-puct queue jobs."""

import logging

from nodalpulse.crawlers.puct import PuctCrawler
from nodalpulse.db.filings import get_last_crawled_at, get_source_id, upsert_filing
from nodalpulse.queue.pg_queue import enqueue
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


async def handle_crawl_puct(payload: dict) -> dict:
    since = payload.get("since") or await get_last_crawled_at("puct")
    logger.info("handle_crawl_puct since=%s", since)

    source_id = await get_source_id("puct")
    if not source_id:
        raise RuntimeError("source 'puct' not found — schema.sql may not have run")

    crawler = PuctCrawler()
    filings = await crawler.fetch_new(since=since)

    saved = skipped = errors = 0
    for filing in filings:
        try:
            date_parts = filing.filed_at[:10].split("-")
            r2_key = f"raw/puct/{date_parts[0]}/{date_parts[1]}/{date_parts[2]}/{filing.external_id}.{filing.file_ext}"
            ct = CONTENT_TYPES.get(filing.file_ext, "application/octet-stream")
            r2.upload(r2_key, filing.content, ct)

            filing_id = await upsert_filing(filing, source_id, r2_key)
            if filing_id:
                await enqueue(
                    "extract",
                    {"filing_id": filing_id, "r2_key": r2_key, "doc_type": filing.doc_type},
                )
                saved += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Error persisting filing %s", filing.external_id)
            errors += 1

    result = {"saved": saved, "skipped": skipped, "errors": errors, "total": len(filings)}
    logger.info("Crawl complete: %s", result)
    return result
