"""DB operations for filing persistence."""

import json
import logging
from datetime import datetime

from sqlalchemy import text

from nodalpulse.crawlers.base import RawFiling
from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def get_source_id(slug: str) -> str | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id::text FROM sources WHERE slug = :slug"),
            {"slug": slug},
        )
        return result.scalar_one_or_none()


async def get_existing_item_keys(source_id: str, item_keys: list[str]) -> set[str]:
    """Return subset of item_keys already present in filings (metadata->>'item_key')."""
    if not item_keys:
        return set()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT metadata->>'item_key'
                FROM filings
                WHERE source_id = CAST(:source_id AS uuid)
                  AND metadata->>'item_key' = ANY(:item_keys)
            """),
            {"source_id": source_id, "item_keys": item_keys},
        )
        return {row[0] for row in result.fetchall() if row[0]}


async def get_last_crawled_at(source_slug: str) -> str | None:
    """Return ISO date string of the newest filing we have for this source."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT MAX(filed_at)::date::text FROM filings
                WHERE source_id = (SELECT id FROM sources WHERE slug = :slug)
            """),
            {"slug": source_slug},
        )
        return result.scalar_one_or_none()


async def find_or_create_docket(
    source_id: str,
    docket_number: str,
    jurisdiction: str | None = None,
) -> str:
    """Find or create a dockets row for this source + docket_number.

    Returns the docket UUID. Safe under concurrent writes: ON CONFLICT DO UPDATE
    always fires the RETURNING clause.

    jurisdiction: stamped on INSERT; backfills NULL rows on conflict (safe no-op
    for rows already carrying a jurisdiction value).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO dockets (source_id, external_id, status, jurisdiction)
                VALUES (CAST(:source_id AS uuid), :external_id, 'open', :jurisdiction)
                ON CONFLICT (source_id, external_id) DO UPDATE
                  SET updated_at  = NOW(),
                      jurisdiction = COALESCE(dockets.jurisdiction, EXCLUDED.jurisdiction)
                RETURNING id::text
            """),
            {"source_id": source_id, "external_id": docket_number, "jurisdiction": jurisdiction},
        )
        docket_id = result.scalar_one()
        await session.commit()
        return docket_id


async def get_ferc_docket_set() -> set[str]:
    """Return docket external_ids for the shared FERC + CAISO-FERC watch set.

    Scoped to 'FERC' and 'CAISO-FERC' only. PJM-FERC is intentionally excluded:
    PJM uses handle_crawl_pjm / get_pjm_ferc_docket_set() so that PJM filings are
    stored exclusively under the 'pjm' source. Including PJM-FERC here would cause
    the same FERC accession number to be inserted under both 'ferc' and 'pjm' source
    IDs — a genuine duplicate since both sources pull from the same RSS feed.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT external_id
                FROM dockets
                WHERE jurisdiction IN ('FERC', 'CAISO-FERC')
            """),
        )
        return {row[0] for row in result.fetchall()}


async def get_pjm_ferc_docket_set() -> set[str]:
    """Return PJM-FERC docket external_ids for the PJM RSS watch set.

    Scoped to jurisdiction='PJM-FERC' only. Used by handle_crawl_pjm to build
    a PJM-filtered FercAdapter instance without including CAISO or bare-FERC dockets.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT external_id FROM dockets WHERE jurisdiction = 'PJM-FERC'"),
        )
        return {row[0] for row in result.fetchall()}


async def upsert_filing_dockets(
    filing_id: str,
    docket_ids: list[str],
    first_is_primary: bool = True,
) -> None:
    """Write filing_dockets junction rows for all co-captioned dockets.

    When first_is_primary=True (default): docket_ids[0] is is_primary=True, rest False.
    When first_is_primary=False: all rows are is_primary=False (used for secondary
    cross-refs added at extraction time, e.g. CPUC proceeding refs from CAISO filings).
    Idempotent: ON CONFLICT DO NOTHING (is_primary set on first INSERT).
    """
    if not docket_ids:
        return
    async with AsyncSessionLocal() as session:
        for i, docket_id in enumerate(docket_ids):
            await session.execute(
                text("""
                    INSERT INTO filing_dockets (filing_id, docket_id, is_primary)
                    VALUES (CAST(:filing_id AS uuid), CAST(:docket_id AS uuid), :is_primary)
                    ON CONFLICT (filing_id, docket_id) DO NOTHING
                """),
                {
                    "filing_id": filing_id,
                    "docket_id": docket_id,
                    "is_primary": (i == 0) if first_is_primary else False,
                },
            )
        await session.commit()


async def upsert_filing(
    raw: RawFiling,
    source_id: str,
    r2_key: str | None,  # None when R2 upload is deferred to extraction time (e.g. FERC adapter)
    docket_id: str | None = None,
) -> str | None:
    """Insert filing row. Skips on conflict (same source + external_id). Returns UUID or None.

    If docket_id is provided and the filing already exists with docket_id IS NULL,
    silently backfills docket_id so re-crawled filings get linked without a separate pass.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO filings (
                    source_id, external_id, doc_type, title, filer,
                    filed_at, r2_key, file_ext, source_url, metadata, docket_id
                ) VALUES (
                    CAST(:source_id AS uuid), :external_id, :doc_type, :title, :filer,
                    CAST(:filed_at AS timestamptz), :r2_key, :file_ext, :source_url,
                    CAST(:metadata AS jsonb), CAST(:docket_id AS uuid)
                )
                ON CONFLICT (source_id, external_id) DO NOTHING
                RETURNING id::text
            """),
            {
                "source_id": source_id,
                "external_id": raw.external_id,
                "doc_type": raw.doc_type,
                "title": raw.title[:500],
                "filer": raw.metadata.get("filer", "")[:500],
                "filed_at": datetime.fromisoformat(raw.filed_at),
                "r2_key": r2_key,
                "file_ext": raw.file_ext,
                "source_url": raw.source_url,
                "metadata": json.dumps(raw.metadata),
                "docket_id": docket_id,
            },
        )
        filing_id = result.scalar_one_or_none()

        if not filing_id and docket_id:
            # Existing filing — backfill docket_id only if still NULL (zero-cost no-op otherwise)
            await session.execute(
                text("""
                    UPDATE filings SET docket_id = CAST(:docket_id AS uuid)
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND external_id = :external_id
                      AND docket_id IS NULL
                """),
                {"docket_id": docket_id, "source_id": source_id, "external_id": raw.external_id},
            )

        await session.commit()
        if filing_id:
            logger.debug("Inserted filing %s → %s", raw.external_id, filing_id)
        return filing_id
