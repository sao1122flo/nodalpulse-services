"""Job handler for crawl-ercot queue jobs.

Runs both NPRR and Market Notices crawlers sequentially in one job.
Payload: {"since": "2026-05-01"}  (optional; defaults to last-crawled date per source)
"""

import logging
import os
from datetime import date, timedelta

from nodalpulse.crawlers.ercot_mns import ErcotMarketNoticesCrawler
from nodalpulse.crawlers.ercot_nprr import ErcotNprrCrawler
from nodalpulse.db.filings import get_last_crawled_at, get_source_id, upsert_filing
from nodalpulse.queue.pg_queue import enqueue
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

MAX_LOOKBACK_DAYS = int(os.environ.get("WORKER_MAX_LOOKBACK_DAYS", "3"))
EXTRACTION_MODE = os.environ.get("EXTRACTION_MODE", "on-demand")

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


async def _run_crawler(crawler, source_slug: str, since: str | None) -> dict:
    raw_since = since or await get_last_crawled_at(source_slug)
    earliest = (date.today() - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
    effective_since = max(raw_since, earliest) if raw_since else earliest
    source_id = await get_source_id(source_slug)
    if not source_id:
        raise RuntimeError(f"source '{source_slug}' not found — run services_schema.sql")

    filings = await crawler.fetch_new(since=effective_since)
    saved = skipped = errors = 0

    for filing in filings:
        try:
            date_parts = filing.filed_at[:10].split("-")
            r2_key = (
                f"raw/{source_slug}/{date_parts[0]}/{date_parts[1]}/"
                f"{date_parts[2]}/{filing.external_id}.{filing.file_ext}"
            )
            ct = CONTENT_TYPES.get(filing.file_ext, "application/octet-stream")
            r2.upload(r2_key, filing.content, ct)

            filing_id = await upsert_filing(filing, source_id, r2_key)
            if filing_id:
                if EXTRACTION_MODE == "proactive":
                    await enqueue(
                        "extract",
                        {"filing_id": filing_id, "r2_key": r2_key, "doc_type": filing.doc_type},
                    )
                saved += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Error persisting %s filing %s", source_slug, filing.external_id)
            errors += 1

    return {"source": source_slug, "saved": saved, "skipped": skipped, "errors": errors}


async def handle_crawl_ercot(payload: dict) -> dict:
    since = payload.get("since")
    logger.info("handle_crawl_ercot since=%s", since)

    nprr_result = await _run_crawler(ErcotNprrCrawler(), "ercot-nprr", since)
    mn_result = await _run_crawler(ErcotMarketNoticesCrawler(), "ercot-mn", since)

    result = {
        "nprr": nprr_result,
        "market_notices": mn_result,
        "total_saved": nprr_result["saved"] + mn_result["saved"],
    }
    logger.info("ERCOT crawl complete: %s", result)
    return result
