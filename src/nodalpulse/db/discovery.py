"""DB operations for discovery_feed and watched_entities (issue #85)."""

import logging
from datetime import date

from sqlalchemy import text

from nodalpulse.crawlers.ferc_discovery import DiscoveryItem
from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def upsert_discovery_items(items: list[DiscoveryItem]) -> tuple[int, int]:
    """Insert discovery_feed rows. Idempotent: ON CONFLICT (accession) DO NOTHING.

    Returns (saved, skipped).
    """
    if not items:
        return 0, 0

    saved = skipped = 0
    async with AsyncSessionLocal() as session:
        for item in items:
            result = await session.execute(
                text("""
                    INSERT INTO discovery_feed
                      (accession, jurisdiction, description, filer_names,
                       docket_numbers, filed_at, doc_type)
                    VALUES
                      (:accession, :jurisdiction, :description, :filer_names,
                       :docket_numbers, CAST(:filed_at AS date), :doc_type)
                    ON CONFLICT (accession) DO NOTHING
                    RETURNING id
                """),
                {
                    "accession": item.accession,
                    "jurisdiction": item.jurisdiction,
                    "description": item.description[:2000],
                    "filer_names": item.filer_names,
                    "docket_numbers": item.docket_numbers,
                    "filed_at": item.filed_at,
                    "doc_type": item.doc_type,
                },
            )
            if result.scalar_one_or_none():
                saved += 1
            else:
                skipped += 1
        await session.commit()

    logger.info("upsert_discovery_items: saved=%d skipped=%d", saved, skipped)
    return saved, skipped


async def cleanup_expired_discovery() -> int:
    """Delete rows past their 30-day TTL. Returns count deleted."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("DELETE FROM discovery_feed WHERE expires_at < NOW() RETURNING id")
        )
        deleted = len(result.fetchall())
        await session.commit()

    if deleted:
        logger.info("cleanup_expired_discovery: deleted %d expired rows", deleted)
    return deleted


# ── Entity-match engine ───────────────────────────────────────────────────────


async def get_watched_entity_patterns(user_id: str) -> list[str]:
    """Return ILIKE patterns for a user's watched entities + aliases.

    Each canonical name and alias becomes a '%name%' pattern.
    Returns empty list if no entities registered (match engine is skipped).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT name, aliases
                FROM watched_entities
                WHERE user_id = CAST(:uid AS uuid)
                ORDER BY created_at
            """),
            {"uid": user_id},
        )
        patterns: list[str] = []
        for row in result.fetchall():
            name, aliases = row[0], row[1] or []
            patterns.append(f"%{name}%")
            for alias in aliases:
                if alias:
                    patterns.append(f"%{alias}%")
        return patterns


async def get_discovery_hits(
    patterns: list[str],
    since_date: date,
    until_date: date | None = None,
    limit: int = 20,
) -> list[dict]:
    """Query discovery_feed for entity-match hits.

    Match is description-primary (tested in dogfood #129), filer_names-secondary
    (AUTHOR-type only — see ferc_discovery.py comment on why not the full array).

    patterns: ILIKE patterns like ['%Hecate Energy%', '%Hecate Energy LLC%']
    Returns list of dicts ordered by filed_at DESC.
    matched_on: 'description' | 'filer_name' — for diagnostics in verify step.
    """
    if not patterns:
        return []

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    accession,
                    description,
                    filer_names,
                    docket_numbers,
                    filed_at::text,
                    doc_type,
                    CASE
                        WHEN description ILIKE ANY(:patterns) THEN 'description'
                        ELSE 'filer_name'
                    END AS matched_on
                FROM discovery_feed
                WHERE expires_at > NOW()
                  AND filed_at >= CAST(:since AS date)
                  AND (:until IS NULL OR filed_at <= CAST(:until AS date))
                  AND (
                      description ILIKE ANY(:patterns)
                      OR EXISTS (
                          SELECT 1 FROM unnest(filer_names) AS fn
                          WHERE fn ILIKE ANY(:patterns)
                      )
                  )
                ORDER BY filed_at DESC
                LIMIT :lim
            """),
            {
                "patterns": patterns,
                "since": since_date,
                "until": until_date,
                "lim": limit,
            },
        )
        hits = [dict(r) for r in result.mappings().fetchall()]

    logger.info(
        "get_discovery_hits: patterns=%d since=%s hits=%d",
        len(patterns), since_date, len(hits),
    )
    return hits
