"""Job handler for crawl-puct queue jobs."""

import logging
import os
from datetime import date, timedelta

import httpx

from nodalpulse.crawlers.puct import PuctCrawler
from nodalpulse.db.filings import find_or_create_docket, get_last_crawled_at, get_source_id, upsert_filing
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

_HTTP_HEADERS = {"User-Agent": "NodalPulse/1.0 regulatory-monitor"}


async def handle_crawl_puct(payload: dict) -> dict:
    raw_since = payload.get("since") or await get_last_crawled_at("puct")
    earliest = (date.today() - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
    since = max(raw_since, earliest) if raw_since else earliest
    logger.info("handle_crawl_puct since=%s (max_lookback=%d days)", since, MAX_LOOKBACK_DAYS)

    source_id = await get_source_id("puct")
    if not source_id:
        raise RuntimeError("source 'puct' not found — schema.sql may not have run")

    crawler = PuctCrawler()

    # Phase 1: resolve all document URLs (metadata only, no content in RAM)
    rows = await crawler.get_rows(since=since)

    # Phase 2: download → upload → save one file at a time to stay within memory limits
    saved = skipped = errors = 0
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, verify=False, headers=_HTTP_HEADERS
    ) as client:
        for row in rows:
            try:
                filing = await crawler._download_filing(client, row)
                if not filing:
                    continue
                date_parts = filing.filed_at[:10].split("-")
                r2_key = f"raw/puct/{date_parts[0]}/{date_parts[1]}/{date_parts[2]}/{filing.external_id}.{filing.file_ext}"
                ct = CONTENT_TYPES.get(filing.file_ext, "application/octet-stream")
                r2.upload(r2_key, filing.content, ct)

                control_number = filing.metadata.get("control_number")
                docket_id = (
                    await find_or_create_docket(source_id, control_number)
                    if control_number else None
                )
                filing_id = await upsert_filing(filing, source_id, r2_key, docket_id=docket_id)
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
                logger.exception("Error persisting filing %s", row.get("external_id", "?"))
                errors += 1

    result = {"saved": saved, "skipped": skipped, "errors": errors, "total": len(rows)}
    logger.info("Crawl complete: %s", result)
    return result
