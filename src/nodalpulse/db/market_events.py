"""DB access layer for market_events — non-document calendar deadline rows."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def upsert_market_event(
    *,
    source: str,
    jurisdiction: str,
    event_type: str,
    title: str,
    event_date: date,
    estimated: bool,
    related_docket: str | None = None,
    source_url: str | None = None,
    external_id: str | None = None,
) -> bool:
    """Insert a market event row. Idempotent by external_id (ON CONFLICT DO NOTHING).

    Returns True if inserted, False if the row already existed.
    external_id should be a stable slug derived from source + event date + title.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO market_events
                    (source, jurisdiction, event_type, title, event_date, estimated,
                     related_docket, source_url, external_id)
                VALUES
                    (:source, :jurisdiction, :event_type, :title, :event_date, :estimated,
                     :related_docket, :source_url, :external_id)
                ON CONFLICT (external_id) DO NOTHING
                RETURNING id
            """),
            {
                "source": source,
                "jurisdiction": jurisdiction,
                "event_type": event_type,
                "title": title[:500],
                "event_date": event_date,
                "estimated": estimated,
                "related_docket": related_docket,
                "source_url": source_url,
                "external_id": external_id,
            },
        )
        inserted = result.scalar_one_or_none() is not None
        await session.commit()
        return inserted


async def get_market_events(
    jurisdiction: str,
    from_date: date,
    until_date: date,
) -> list[dict]:
    """Return market events for a jurisdiction in [from_date, until_date] ordered by event_date.

    Used by the brief composer to surface calendar deadlines alongside filing extractions.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    id::text, source, jurisdiction, event_type, title,
                    event_date::text, estimated, related_docket, source_url, external_id
                FROM market_events
                WHERE jurisdiction = :jurisdiction
                  AND event_date >= :from_date
                  AND event_date <= :until_date
                ORDER BY event_date ASC
            """),
            {"jurisdiction": jurisdiction, "from_date": from_date, "until_date": until_date},
        )
        return [dict(row._mapping) for row in result.fetchall()]
