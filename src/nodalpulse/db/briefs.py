"""DB operations for brief generation and persistence."""

import logging
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _pg_uuid_array(uuids: list[str]) -> str:
    """Format validated UUIDs as a PostgreSQL array literal, safe to inline in SQL."""
    safe = [u for u in uuids if _UUID_RE.match(u)]
    return "'{" + ",".join(safe) + "}'::uuid[]"


async def get_active_user_ids() -> list[str]:
    """Return user IDs eligible for daily briefs (entitlement + active subscription + profile)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT DISTINCT u.id::text
            FROM users u
            JOIN user_profiles up ON up.user_id = u.id
            JOIN entitlements e ON e.user_id = u.id
                AND e.feature = 'daily-brief'
                AND (e.expires_at IS NULL OR e.expires_at > NOW())
            JOIN subscriptions s ON s.user_id = u.id
                AND s.status IN ('active', 'trialing')
                AND (s.current_period_end IS NULL OR s.current_period_end > NOW())
            WHERE u.email_verified = true
        """))
        return [row[0] for row in result.fetchall()]


async def get_user_for_brief(user_id: str) -> dict | None:
    """Return full user+profile row for a single user, None if no entitlement."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    u.id::text       AS user_id,
                    u.email,
                    u.name,
                    up.market_roles,
                    up.tracked_docket_ids::text[] AS tracked_docket_ids,
                    up.tracked_tags,
                    up.email_format
                FROM users u
                JOIN user_profiles up ON up.user_id = u.id
                JOIN entitlements e ON e.user_id = u.id
                    AND e.feature = 'daily-brief'
                    AND (e.expires_at IS NULL OR e.expires_at > NOW())
                JOIN subscriptions s ON s.user_id = u.id
                    AND s.status IN ('active', 'trialing')
                    AND (s.current_period_end IS NULL OR s.current_period_end > NOW())
                WHERE u.id = CAST(:uid AS uuid)
                  AND u.email_verified = true
            """),
            {"uid": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def get_last_brief_date(user_id: str) -> date | None:
    """Most recent brief date for a user, or None if no briefs yet."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT MAX(date) FROM briefs WHERE user_id = CAST(:uid AS uuid)"),
            {"uid": user_id},
        )
        return result.scalar_one_or_none()


async def get_filings_for_brief(since: datetime, until: datetime) -> list[dict]:
    """All filings+extractions in the window, excluding haiku-irrelevant ones."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    f.id::text          AS filing_id,
                    f.doc_type,
                    f.title,
                    f.filer,
                    f.filed_at,
                    f.r2_key,
                    f.source_url,
                    f.metadata,
                    e.id::text          AS extraction_id,
                    e.payload           AS extraction_payload,
                    e.haiku_verdict
                FROM filings f
                JOIN extractions e ON e.filing_id = f.id
                WHERE f.filed_at >= :since
                  AND f.filed_at < :until
                  AND e.haiku_verdict != 'irrelevant'
                ORDER BY f.filed_at DESC
            """),
            {"since": since, "until": until},
        )
        return [dict(row) for row in result.mappings().fetchall()]


async def check_eval_gate() -> bool:
    """True if the last eval run passed, or if no runs exist (assume ok for MVP)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT passed FROM eval_runs ORDER BY run_at DESC LIMIT 1")
        )
        row = result.first()
        return row is None or bool(row[0])


async def get_already_enqueued_for_date(brief_date: date) -> set[str]:
    """User IDs that already have a brief row or a pending/running compose-brief job for today."""
    async with AsyncSessionLocal() as session:
        r1 = await session.execute(
            text("SELECT user_id::text FROM briefs WHERE date = :d"),
            {"d": brief_date},
        )
        r2 = await session.execute(
            text("""
                SELECT payload->>'user_id'
                FROM jobs
                WHERE kind = 'compose-brief'
                  AND status IN ('pending', 'running')
                  AND (payload->>'brief_date') = :d
            """),
            {"d": brief_date.isoformat()},
        )
        ids: set[str] = {row[0] for row in r1.fetchall() if row[0]}
        ids |= {row[0] for row in r2.fetchall() if row[0]}
        return ids


async def insert_brief(
    *,
    user_id: str,
    brief_date: date,
    model: str,
    prompt_ver: str,
    html_r2_key: str | None,
    txt_r2_key: str | None,
    filing_ids: list[str],
    citation_count: int,
    send_status: str,
) -> str:
    filing_ids_literal = _pg_uuid_array(filing_ids)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
                INSERT INTO briefs (
                    user_id, date, model, prompt_ver,
                    html_r2_key, txt_r2_key,
                    filing_ids, citation_count, send_status
                ) VALUES (
                    CAST(:user_id AS uuid), :brief_date, :model, :prompt_ver,
                    :html_r2_key, :txt_r2_key,
                    {filing_ids_literal}, :citation_count, :send_status
                )
                ON CONFLICT (user_id, date) DO UPDATE SET
                    model           = EXCLUDED.model,
                    prompt_ver      = EXCLUDED.prompt_ver,
                    html_r2_key     = EXCLUDED.html_r2_key,
                    txt_r2_key      = EXCLUDED.txt_r2_key,
                    filing_ids      = EXCLUDED.filing_ids,
                    citation_count  = EXCLUDED.citation_count,
                    send_status     = EXCLUDED.send_status
                RETURNING id::text
            """),
            {
                "user_id": user_id,
                "brief_date": brief_date,
                "model": model,
                "prompt_ver": prompt_ver,
                "html_r2_key": html_r2_key,
                "txt_r2_key": txt_r2_key,
                "citation_count": citation_count,
                "send_status": send_status,
            },
        )
        brief_id = result.scalar_one()
        await session.commit()
        return brief_id


async def mark_brief_sent(brief_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE briefs SET sent_at = NOW(), send_status = 'sent' "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": brief_id},
        )
        await session.commit()
