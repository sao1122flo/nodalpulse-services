"""Job handler for refresh-extraction — re-extracts a single filing by ID."""

import logging

from nodalpulse.db.extractions import get_filing
from nodalpulse.workers.extract import handle_extract

logger = logging.getLogger(__name__)


async def handle_refresh_extraction(payload: dict) -> dict:
    filing_id = payload["filing_id"]
    filing = await get_filing(filing_id)
    if not filing:
        raise RuntimeError(f"Filing {filing_id} not found")
    logger.info("refresh-extraction: filing=%s r2_key=%s", filing_id, filing["r2_key"])
    return await handle_extract({
        "filing_id": filing_id,
        "r2_key": filing["r2_key"],
        "doc_type": filing.get("doc_type", "puct-filing"),
    })
