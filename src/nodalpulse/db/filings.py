"""DB operations for filing persistence."""

import json
import logging

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


async def upsert_filing(raw: RawFiling, source_id: str, r2_key: str) -> str | None:
    """Insert filing row. Skips on conflict (same source + external_id). Returns UUID or None."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO filings (
                    source_id, external_id, doc_type, title, filer,
                    filed_at, r2_key, file_ext, source_url, metadata
                ) VALUES (
                    :source_id::uuid, :external_id, :doc_type, :title, :filer,
                    :filed_at::timestamptz, :r2_key, :file_ext, :source_url, :metadata::jsonb
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
                "filed_at": raw.filed_at,
                "r2_key": r2_key,
                "file_ext": raw.file_ext,
                "source_url": raw.source_url,
                "metadata": json.dumps(raw.metadata),
            },
        )
        filing_id = result.scalar_one_or_none()
        await session.commit()
        if filing_id:
            logger.debug("Inserted filing %s → %s", raw.external_id, filing_id)
        return filing_id
