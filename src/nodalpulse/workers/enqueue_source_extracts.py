"""Job handler: enqueue extract jobs for un-extracted filings of a metadata-only source.

Some sources are crawled as metadata only (no document body pulled at crawl time)
and, under EXTRACTION_MODE=selective, are never extracted because no docket is
tracked — so their record pages never populate. Examples: FERC (fetched via
DownloadP8File / ferc_file_id) and the PJM-state PUCs vascc / mdpsc / njbpu
(fetched via a direct source_url PDF).

The scheduler enqueues this job once per weekday per source; the handler enqueues
'extract' jobs for that source's un-extracted, fetchable, non-'(doc-less)'
filings. `since_days` bounds the window (the state PUCs have 1k-7k historical
filings; extracting all of them is a large one-off cost, so daily runs cover only
a recent window — the deep historical backfill is a separate, deliberate op).

payload:
  slug        (str, required)   — source slug, e.g. 'vascc'
  since_days  (int, optional)   — only filings filed within the last N days
  cap         (int, optional)   — max jobs to enqueue this run (default 200)
"""

import logging

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULT_CAP = 200


async def handle_enqueue_source_extracts(payload: dict) -> dict:
    slug = payload["slug"]
    since_days = payload.get("since_days")
    cap = int(payload.get("cap") or DEFAULT_CAP)

    params: dict = {"slug": slug, "docless": "(doc-less)%", "cap": cap}
    since_clause = ""
    if since_days is not None:
        since_clause = "AND f.filed_at >= now() - make_interval(days => :since_days)"
        params["since_days"] = int(since_days)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"""
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
                WHERE s.slug = :slug
                  AND f.r2_key IS NULL
                  AND (
                        (f.source_url IS NOT NULL AND f.source_url <> '')
                        OR f.metadata ->> 'ferc_file_id' IS NOT NULL
                  )
                  AND f.title NOT ILIKE :docless
                  {since_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM extractions e WHERE e.filing_id = f.id
                  )
                  -- No extract job of ANY status. A filing that was processed but
                  -- yielded no extraction (download returned non-extractable
                  -- content — e.g. MD-PSC's maillogpdfview viewer URLs return HTML,
                  -- not a PDF) keeps r2_key NULL and has no extraction row; matching
                  -- only 'pending' here would re-enqueue it every single day forever.
                  -- The queue already retries transient failures within a job's
                  -- budget, so one terminal attempt per filing is correct.
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.kind = 'extract'
                        AND j.payload ->> 'filing_id' = f.id::text
                  )
                ORDER BY f.filed_at DESC
                LIMIT :cap
                RETURNING 1
                """
            ),
            params,
        )
        enqueued = len(result.fetchall())
        await session.commit()

    logger.info(
        "enqueue-source-extracts: slug=%s since_days=%s enqueued=%d",
        slug,
        since_days,
        enqueued,
    )
    return {"status": "ok", "slug": slug, "enqueued": enqueued}
