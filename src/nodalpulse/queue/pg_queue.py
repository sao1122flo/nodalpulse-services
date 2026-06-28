"""Postgres-backed job queue using FOR UPDATE SKIP LOCKED."""

import asyncio
import json
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.llm.client import CreditExhaustedError

logger = logging.getLogger(__name__)
WORKER_ID = socket.gethostname()

_SPEND_CIRCUIT_USD = float(os.environ.get("WORKER_SPEND_CIRCUIT_USD_PER_HOUR", "5.0"))


async def _last_hour_spend_usd() -> float:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COALESCE(SUM(cost_usd_estimate), 0)
                FROM llm_calls
                WHERE created_at >= NOW() - INTERVAL '1 hour'
            """)
        )
        return float(result.scalar_one() or 0)


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


async def enqueue_idempotent(
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    priority: int = 0,
) -> tuple[str, bool]:
    """Insert a job unless one with the same idempotency_key already exists.

    Returns (job_id, created) — created=False means the key was already present
    and the existing job_id is returned. First-write-wins; no 409 raised.
    """
    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            text("SELECT id::text FROM jobs WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        )
        row = existing.first()
        if row:
            return row[0], False

        result = await session.execute(
            text(
                """
                INSERT INTO jobs (kind, payload, priority, idempotency_key)
                VALUES (:kind, CAST(:payload AS JSONB), :priority, :key)
                RETURNING id::text
                """
            ),
            {
                "kind": kind,
                "payload": json.dumps(payload),
                "priority": priority,
                "key": idempotency_key,
            },
        )
        job_id = result.scalar_one()
        await session.commit()
        return job_id, True


async def dequeue(kind: str, lock_seconds: int = 900) -> dict[str, Any] | None:
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
                      AND (
                          (status = 'pending' AND run_after <= NOW())
                          OR (status = 'running' AND locked_until < NOW())
                      )
                    ORDER BY priority DESC, run_after ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id::text, kind, payload, attempts, max_attempts
                """
            ),
            {
                "worker_id": WORKER_ID,
                "lock_interval": timedelta(seconds=lock_seconds),
                "kind": kind,
            },
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


async def fail(job_id: str, error: str, duration_ms: int, max_attempts: int = 5) -> None:
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
        spent = await _last_hour_spend_usd()
        if spent >= _SPEND_CIRCUIT_USD:
            logger.critical(
                "Spend circuit breaker: $%.4f last-hour >= $%.2f threshold — sleeping 1h",
                spent,
                _SPEND_CIRCUIT_USD,
            )
            await asyncio.sleep(3600)
            continue

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
        except CreditExhaustedError as exc:
            ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
            logger.critical(
                "CreditExhaustedError — worker paused 24h (top up credit then redeploy): %s", exc
            )
            await fail(job["id"], str(exc), ms, max_attempts=job["max_attempts"])
            await asyncio.sleep(86400)
            continue
        except Exception as exc:
            ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
            logger.exception("Job %s failed: %s", job["id"], exc)
            await fail(job["id"], str(exc), ms, max_attempts=job["max_attempts"])
