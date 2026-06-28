"""Persistent state for the daily scheduler.

Backed by scheduler_daily_runs (migration: sql/add_scheduler_daily_runs.sql).
Each row tracks whether crawl and brief jobs have been enqueued for a given
CT calendar date. NULL columns mean "not yet enqueued for that date."

All writes use INSERT ... ON CONFLICT DO UPDATE (UPSERT) so concurrent
startups cannot trample each other — the last writer wins, which is safe
because both mark the same date as done.
"""

from datetime import date

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal


async def is_crawl_done_for(d: date) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT crawl_enqueued_at FROM scheduler_daily_runs WHERE run_date = :d"),
            {"d": d},
        )
        row = result.first()
        return row is not None and row[0] is not None


async def mark_crawl_done_for(d: date) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO scheduler_daily_runs (run_date, crawl_enqueued_at) "
                "VALUES (:d, NOW()) "
                "ON CONFLICT (run_date) DO UPDATE SET crawl_enqueued_at = NOW()"
            ),
            {"d": d},
        )
        await session.commit()


async def is_brief_done_for(d: date) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT brief_enqueued_at FROM scheduler_daily_runs WHERE run_date = :d"),
            {"d": d},
        )
        row = result.first()
        return row is not None and row[0] is not None


async def mark_brief_done_for(d: date) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO scheduler_daily_runs (run_date, brief_enqueued_at) "
                "VALUES (:d, NOW()) "
                "ON CONFLICT (run_date) DO UPDATE SET brief_enqueued_at = NOW()"
            ),
            {"d": d},
        )
        await session.commit()
