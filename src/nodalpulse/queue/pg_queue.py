"""Postgres-backed job queue using FOR UPDATE SKIP LOCKED."""

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)
WORKER_ID = socket.gethostname()


async def enqueue(kind: str, payload: dict[str, Any], priority: int = 0) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO jobs (kind, payload, priority)
                VALUES (:kind, CAST(:payload AS JSONB), :priority)
                RETURNING id::text
                """
            ),
            {"kind": kind, "payload": json.dumps(payload), "priority": priority},
        )
        job_id = result.scalar_one()
        await session.commit()
        return job_id


async def dequeue(kind: str, lock_seconds: int = 60) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE jobs SET
                    status = 'running',
                    locked_by = :worker_id,
                    locked_until = NOW() + :lock_interval,
                    attempts = attempts + 1,
                    updated_at = NOW()
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE kind = :kind
                      AND status = 'pending'
                      AND run_after <= NOW()
                    ORDER BY priority DESC, run_after ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id::text, kind, payload, attempts
                """
            ),
            {"worker_id": WORKER_ID, "lock_interval": timedelta(seconds=lock_seconds), "kind": kind},
        )
        row = result.mappings().first()
        await session.commit()
        return dict(row) if row else None


async def complete(job_id: str, output: dict[str, Any], duration_ms: int) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE jobs SET status='done', updated_at=NOW() WHERE id=:id"),
            {"id": job_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO job_results (job_id, attempt, success, output, duration_ms)
                SELECT id, attempts, true, CAST(:output AS JSONB), :duration_ms FROM jobs WHERE id=:id
                """
            ),
            {"id": job_id, "output": json.dumps(output), "duration_ms": duration_ms},
        )
        await session.commit()


async def fail(job_id: str, error: str, duration_ms: int, max_attempts: int = 3) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE jobs SET
                    status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'pending' END,
                    error = :error,
                    locked_by = NULL,
                    locked_until = NULL,
                    run_after = NOW() + (attempts * interval '30 seconds'),
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"id": job_id, "error": error, "max_attempts": max_attempts},
        )
        await session.execute(
            text(
                """
                INSERT INTO job_results (job_id, attempt, success, output, duration_ms)
                SELECT id, attempts, false, CAST(:output AS JSONB), :duration_ms FROM jobs WHERE id=:id
                """
            ),
            {"id": job_id, "output": json.dumps({"error": error}), "duration_ms": duration_ms},
        )
        await session.commit()


async def run_worker(kind: str, handler: Any, poll_interval: float = 5.0) -> None:
    logger.info("Worker started: kind=%s worker_id=%s", kind, WORKER_ID)
    while True:
        job = await dequeue(kind)
        if job is None:
            await asyncio.sleep(poll_interval)
            continue

        start = datetime.now(UTC)
        logger.info("Processing job %s", job["id"])
        try:
            output = await handler(job["payload"])
            ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
            await complete(job["id"], output or {}, ms)
            logger.info("Job %s done in %dms", job["id"], ms)
        except Exception as exc:
            ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
            logger.exception("Job %s failed: %s", job["id"], exc)
            await fail(job["id"], str(exc), ms)
