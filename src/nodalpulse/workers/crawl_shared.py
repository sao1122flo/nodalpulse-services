"""Source-agnostic persist loop used by all market crawl handlers."""

import logging
import os
from datetime import date, timedelta

from nodalpulse.crawlers.base import MarketAdapter
from nodalpulse.db.filings import (
    find_or_create_docket,
    get_all_tracked_docket_ids,
    get_last_crawled_at,
    get_source_id,
    upsert_filing,
    upsert_filing_dockets,
)
from nodalpulse.queue.pg_queue import enqueue
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

MAX_LOOKBACK_DAYS = int(os.environ.get("WORKER_MAX_LOOKBACK_DAYS", "3"))
EXTRACTION_MODE = os.environ.get("EXTRACTION_MODE", "selective")

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}

# Canonical jurisdiction string stamped on new dockets at crawl time.
# Add new source slugs here as markets are onboarded.
_SOURCE_JURISDICTION: dict[str, str] = {
    "puct": "PUCT",
    "ercot-nprr": "ERCOT",
    "ercot-mn": "ERCOT",
    "tlo": "PUCT",
    "ferc": "FERC",
    "caiso": "CAISO-FERC",  # T5
    "pjm": "PJM-FERC",  # T8
    "imm": "PJM-FERC",  # T9 — IMM files at FERC; dockets are PJM-FERC
    "cpuc": "CPUC",  # #79
}


async def run_adapter(
    adapter: MarketAdapter,
    source_slug: str,
    since: str | None,
    max_filings: int | None = None,
) -> dict:
    """Fetch, persist, and optionally enqueue extraction for all new filings.

    Docket linkage (multi-docket aware):
    - metadata["docket_numbers"] (list)  -- FERC multi-caption filings (primary path)
    - metadata["control_number"] (str)   -- PUCT single-docket filings
    - metadata["docket_number"] (str)    -- legacy single-docket fallback

    For multi-docket filings: find_or_create_docket is called for EVERY referenced
    docket so all rows exist in the dockets table. filings.docket_id is set to the
    first/primary docket; filing_dockets junction rows are written for all dockets.

    R2 upload: skipped when filing.content is empty (deferred adapters such as FERC
    that store source_url only at crawl time and upload to R2 at extraction time).

    max_filings: on-demand mode — cap the filings list to the N most recent after
    fetch. Also bypasses MAX_LOOKBACK_DAYS so the caller's since date is respected
    as-is (required for backfills that must reach beyond the 3-day rolling window).
    """
    if max_filings is not None and since is not None:
        # On-demand: caller sets an appropriate since floor; skip the lookback cap.
        effective_since = since
    else:
        raw_since = since or await get_last_crawled_at(source_slug)
        earliest = (date.today() - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
        effective_since = max(raw_since, earliest) if raw_since else earliest

    source_id = await get_source_id(source_slug)
    if not source_id:
        raise RuntimeError(f"source '{source_slug}' not found -- run services_schema.sql")

    jurisdiction = _SOURCE_JURISDICTION.get(source_slug)
    logger.info(
        "run_adapter source=%s since=%s jurisdiction=%s max_filings=%s",
        source_slug,
        effective_since,
        jurisdiction,
        max_filings,
    )
    filings = await adapter.fetch_new(since=effective_since)
    logger.info("run_adapter source=%s fetched=%d", source_slug, len(filings))

    if max_filings is not None and len(filings) > max_filings:
        logger.info(
            "run_adapter source=%s on-demand cap: keeping %d of %d (most recent first)",
            source_slug,
            max_filings,
            len(filings),
        )
        filings = filings[:max_filings]

    # Load tracked docket set once per tick; empty set means no extractions in selective mode.
    tracked_set: set[str] = set()
    if EXTRACTION_MODE == "selective":
        tracked_set = await get_all_tracked_docket_ids()
        logger.info("run_adapter selective: %d tracked dockets", len(tracked_set))

    saved = skipped = errors = 0
    for filing in filings:
        try:
            date_parts = filing.filed_at[:10].split("-")
            r2_key = (
                f"raw/{source_slug}/{date_parts[0]}/{date_parts[1]}/"
                f"{date_parts[2]}/{filing.external_id}.{filing.file_ext}"
            )

            # Upload to R2 only when content is present; deferred adapters (e.g. FERC)
            # store source_url and upload at extraction time to spare R2 Class A writes.
            persisted_r2_key: str | None = None
            if filing.content:
                await r2.upload_async(
                    r2_key,
                    filing.content,
                    _CONTENT_TYPES.get(filing.file_ext, "application/octet-stream"),
                )
                persisted_r2_key = r2_key

            # Build docket ref list. metadata["docket_numbers"] (list) takes priority;
            # fall back to singular control_number / docket_number for older adapters.
            docket_refs: list[str] = (
                filing.metadata.get("docket_numbers")
                or (
                    [filing.metadata["control_number"]]
                    if filing.metadata.get("control_number")
                    else []
                )
                or (
                    [filing.metadata["docket_number"]]
                    if filing.metadata.get("docket_number")
                    else []
                )
            )

            # Ensure every referenced docket exists; primary docket -> filings.docket_id.
            # jurisdiction + title are stamped on INSERT and backfill NULL rows on conflict.
            # title is only passed for the primary docket (index 0); secondary cross-ref
            # dockets get their own titles when their primary filings are crawled.
            docket_ids: list[str] = []
            for i, ref in enumerate(docket_refs):
                created = await find_or_create_docket(
                    source_id,
                    ref,
                    jurisdiction=jurisdiction,
                    title=filing.title if i == 0 else None,
                )
                docket_ids.append(created)
            docket_id = docket_ids[0] if docket_ids else None
            if len(docket_refs) > 1:
                logger.debug(
                    "Filing %s captions %d dockets; primary -> %s",
                    filing.external_id,
                    len(docket_refs),
                    docket_refs[0],
                )

            filing_id = await upsert_filing(
                filing, source_id, persisted_r2_key, docket_id=docket_id
            )
            if filing_id:
                if docket_ids:
                    await upsert_filing_dockets(filing_id, docket_ids)
                should_extract = EXTRACTION_MODE == "proactive" or (
                    EXTRACTION_MODE == "selective" and any(did in tracked_set for did in docket_ids)
                )
                if should_extract:
                    await enqueue(
                        "extract",
                        {
                            "filing_id": filing_id,
                            "r2_key": persisted_r2_key,
                            "doc_type": filing.doc_type,
                        },
                    )
                saved += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Error persisting %s filing %s", source_slug, filing.external_id)
            errors += 1

    return {"source": source_slug, "saved": saved, "skipped": skipped, "errors": errors}
