"""PJM FERC crawl handler — docket-scoped ingestion via eLibrarywebapi.

Uses FercAdapter(pjm_docket_set) to query AdvancedSearch per watched docket
and delegates to run_adapter for persist/R2/junction/extraction.

Discovery pump (Phase 1 firehose) was removed: the affiliations[] filter on the
FERC API matches service-list parties, not filers only, returning 4.68M results —
unusable for targeted discovery. New PJM dockets are seeded manually, consistent
with all other jurisdictions in Beta.

Jurisdiction stamp: run_adapter uses _SOURCE_JURISDICTION["pjm"] = "PJM-FERC"
(crawl_shared.py). Multi-docket captions and filing_dockets junction rows are
handled by run_adapter exactly as for CAISO.
"""

from __future__ import annotations

import logging

from nodalpulse.crawlers.ferc import FercAdapter
from nodalpulse.db.filings import get_pjm_ferc_docket_set, get_source_id
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)


async def handle_crawl_pjm(payload: dict) -> dict:
    """Crawl FERC eLibrarywebapi for all tracked PJM-FERC dockets."""
    source_id = await get_source_id("pjm")
    if not source_id:
        logger.warning("handle_crawl_pjm: 'pjm' source row not found — apply seed-pjm-source.sql")
        return {"source": "pjm", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}

    pjm_set = await get_pjm_ferc_docket_set()
    if not pjm_set:
        logger.warning(
            "handle_crawl_pjm: PJM docket set empty — apply seed-pjm-dockets.sql to bootstrap"
        )
        return {"source": "pjm", "saved": 0, "skipped": 0, "errors": 0, "watched": 0}

    logger.info("handle_crawl_pjm: %d PJM-FERC dockets in watch set", len(pjm_set))
    result = await run_adapter(FercAdapter(pjm_set), "pjm", payload.get("since"))
    result["watched"] = len(pjm_set)
    logger.info("handle_crawl_pjm complete: %s", result)
    return result
