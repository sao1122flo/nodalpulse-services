"""Temporary one-shot bootstrap handler for PJM Beta verify (T11-verify).

Registered as 'bootstrap-pjm' in worker.py. Enqueue once, worker executes
it on the Railway server (US IP, can reach ecollection.ferc.gov + IMM).
Remove this handler + the HANDLERS entry after the verify passes.

Job payload: {"since": "2026-02-01"}  (defaults to 2026-02-01 if omitted)
"""
from __future__ import annotations

import logging

from nodalpulse.crawlers.ferc import FercAdapter
from nodalpulse.crawlers.imm import ImmAdapter
from nodalpulse.db.filings import (
    find_or_create_docket,
    get_pjm_ferc_docket_set,
    get_source_id,
    upsert_filing,
    upsert_filing_dockets,
)
from nodalpulse.queue.pg_queue import enqueue

logger = logging.getLogger(__name__)


async def handle_bootstrap_pjm(payload: dict) -> dict:
    """Fetch FERC RSS back to since=2026-02-01 + full IMM 2025-2026 catalog.

    No lookback cap — fetches directly without going through run_adapter.
    Persists found filings and enqueues extract jobs.
    """
    since = payload.get("since", "2026-02-01")
    imm_since = payload.get("imm_since", "2025-01-01")

    # ── PJM FERC extended fetch ───────────────────────────────────────────────
    pjm_set = await get_pjm_ferc_docket_set()
    pjm_source_id = await get_source_id("pjm")
    if not pjm_source_id:
        logger.error("bootstrap_pjm: 'pjm' source not found — seed first")
        return {"error": "pjm_source_missing"}

    logger.info("bootstrap_pjm: fetching FERC RSS since=%s, watch=%s", since, sorted(pjm_set))
    pjm_filings = await FercAdapter(pjm_set).fetch_new(since=since)
    logger.info("bootstrap_pjm: FERC RSS returned %d PJM filings", len(pjm_filings))

    pjm_saved = 0
    for raw in pjm_filings:
        docket_refs = raw.metadata.get("docket_numbers") or []
        docket_ids = []
        for ref in docket_refs:
            did = await find_or_create_docket(pjm_source_id, ref, jurisdiction="PJM-FERC")
            docket_ids.append(did)
        filing_id = await upsert_filing(
            raw, pjm_source_id, None,
            docket_id=docket_ids[0] if docket_ids else None,
        )
        if filing_id:
            if docket_ids:
                await upsert_filing_dockets(filing_id, docket_ids)
            await enqueue("extract", {"filing_id": filing_id, "doc_type": raw.doc_type})
            logger.info("bootstrap_pjm: saved PJM filing %s (%s)", raw.external_id, raw.doc_type)
            pjm_saved += 1

    # ── IMM extended fetch ────────────────────────────────────────────────────
    imm_source_id = await get_source_id("imm")
    if not imm_source_id:
        logger.error("bootstrap_pjm: 'imm' source not found — seed first")
        return {"pjm_saved": pjm_saved, "error": "imm_source_missing"}

    logger.info("bootstrap_pjm: fetching IMM since=%s", imm_since)
    imm_filings = await ImmAdapter().fetch_new(since=imm_since)
    logger.info("bootstrap_pjm: IMM returned %d filings", len(imm_filings))

    imm_saved = 0
    for raw in imm_filings:
        docket_refs = raw.metadata.get("docket_numbers") or []
        docket_ids = []
        for ref in docket_refs:
            did = await find_or_create_docket(imm_source_id, ref, jurisdiction="PJM-FERC")
            docket_ids.append(did)
        filing_id = await upsert_filing(
            raw, imm_source_id, None,
            docket_id=docket_ids[0] if docket_ids else None,
        )
        if filing_id:
            if docket_ids:
                await upsert_filing_dockets(filing_id, docket_ids)
            await enqueue("extract", {"filing_id": filing_id, "doc_type": raw.doc_type})
            logger.info("bootstrap_pjm: saved IMM filing %s (%s)", raw.external_id, raw.doc_type)
            imm_saved += 1

    result = {"pjm_saved": pjm_saved, "imm_saved": imm_saved, "pjm_watched": len(pjm_set)}
    logger.info("bootstrap_pjm complete: %s", result)
    return result
