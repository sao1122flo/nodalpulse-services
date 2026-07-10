"""Job handler: enqueue extract jobs for un-extracted FERC filings.

FERC filings are crawled metadata-only (r2_key NULL; a ferc_file_id in metadata)
and, under EXTRACTION_MODE=selective, are never extracted because no FERC docket
is tracked — so FERC record pages would go stale after the initial backfill. The
scheduler enqueues this job once per weekday; it enqueues 'extract' jobs for the
FERC backlog. The extract handler fetches the document via FERC DownloadP8File
(ferc_file_id) when r2_key is NULL, then materializes R2.

Idempotent: skips filings that already have an extraction or a pending extract
job. Excludes '(doc-less)' filings (no document to extract). Capped per run so a
runaway can never flood the queue.
"""

import logging

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Safety cap. Steady-state daily FERC volume is ~1-10 filings; the cap only bites
# on a first run if a backlog exists (it drains over subsequent days).
MAX_PER_RUN = 200


async def handle_enqueue_ferc_extracts(payload: dict) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO jobs (kind, payload, priority, status)
                SELECT
                    'extract',
                    jsonb_build_object(
                        'filing_id', f.id::text,
                        'r2_key',    f.r2_key,
                        'doc_type',  f.doc_type
                    ),
                    5,
                    'pending'
                FROM filings f
                JOIN sources s ON s.id = f.source_id
                WHERE s.slug = 'ferc'
                  AND f.r2_key IS NULL
                  AND f.metadata ->> 'ferc_file_id' IS NOT NULL
                  AND f.title NOT ILIKE :docless
                  AND NOT EXISTS (
                      SELECT 1 FROM extractions e WHERE e.filing_id = f.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.kind = 'extract'
                        AND j.status = 'pending'
                        AND j.payload ->> 'filing_id' = f.id::text
                  )
                ORDER BY f.filed_at DESC
                LIMIT :cap
                RETURNING 1
                """
            ),
            {"docless": "(doc-less)%", "cap": MAX_PER_RUN},
        )
        enqueued = len(result.fetchall())
        await session.commit()
    logger.info("enqueue-ferc-extracts: enqueued %d extract job(s)", enqueued)
    return {"status": "ok", "enqueued": enqueued}
