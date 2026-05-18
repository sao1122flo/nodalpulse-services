import asyncio
import logging
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import text

from nodalpulse.api.auth import verify_bearer
from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date, get_user_exists
from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.db.extractions import get_filing
from nodalpulse.queue.pg_queue import enqueue, enqueue_idempotent

logger = logging.getLogger(__name__)
app = FastAPI(title="nodalpulse-services", version="0.1.0")

_REFRESH_DOCKET_HOURLY_CAP = int(os.environ.get("REFRESH_DOCKET_USER_HOURLY_CAP", "30"))
_REFRESH_DOCKET_MAX_FILINGS = int(os.environ.get("REFRESH_DOCKET_MAX_FILINGS_PER_PIN", "5"))

# TODO: support ERCOT source via explicit source param once ERCOT docket tracking ships
_PUCT_SOURCE_ID = "0725032a-239f-475d-bdd5-251adad3ae05"


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── email endpoints ───────────────────────────────────────────────────────────

@app.get("/unsubscribe/{user_id}", response_class=HTMLResponse)
async def unsubscribe_get(user_id: str) -> HTMLResponse:
    """One-click unsubscribe landing page (GET renders a confirmation form)."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Unsubscribe — NodalPulse</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:80px auto;padding:0 16px;color:#44403C}}</style>
</head><body>
<h2 style="color:#18181B">Unsubscribe from NodalPulse briefs</h2>
<p>Confirm to stop receiving daily briefs.</p>
<form method="post">
  <button type="submit" style="background:#6366F1;color:#fff;border:none;padding:10px 20px;
    border-radius:6px;font-size:14px;cursor:pointer">Unsubscribe</button>
</form>
</body></html>""")


@app.post("/unsubscribe/{user_id}", response_class=HTMLResponse)
async def unsubscribe_post(user_id: str) -> HTMLResponse:
    """One-click unsubscribe POST — sets entitlement expires_at to now."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE entitlements
                SET expires_at = NOW()
                WHERE user_id = CAST(:uid AS uuid) AND feature = 'daily-brief'
            """),
            {"uid": user_id},
        )
        await session.commit()
    logger.info("User %s unsubscribed from daily briefs", user_id)
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Unsubscribed — NodalPulse</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:80px auto;padding:0 16px;color:#44403C}}</style>
</head><body>
<h2 style="color:#18181B">Unsubscribed</h2>
<p>You've been removed from daily briefs. You can re-enable this in your account settings.</p>
</body></html>""")


@app.post("/email/webhooks/brevo")
async def brevo_webhook(request: Request) -> JSONResponse:
    """Brevo event webhook — pauses users on hard bounce or spam complaint.

    Configure in Brevo → Transactional → Webhooks → Events: hard_bounce, complaint.
    """
    try:
        events = await request.json()
        if not isinstance(events, list):
            events = [events]
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    for event in events:
        event_type = event.get("event", "")
        email = event.get("email", "")
        if not email:
            continue

        async with AsyncSessionLocal() as session:
            if event_type == "hard_bounce":
                await session.execute(
                    text("""
                        UPDATE users SET updated_at = NOW()
                        WHERE email = :email
                    """),
                    {"email": email},
                )
                # Expire daily-brief entitlement to stop sending to bounced address
                await session.execute(
                    text("""
                        UPDATE entitlements SET expires_at = NOW()
                        WHERE feature = 'daily-brief'
                          AND user_id = (SELECT id FROM users WHERE email = :email)
                    """),
                    {"email": email},
                )
                await session.commit()
                logger.warning("Hard bounce for %s — daily-brief entitlement expired", email)

            elif event_type == "complaint":
                await session.execute(
                    text("""
                        UPDATE entitlements SET expires_at = NOW()
                        WHERE feature = 'daily-brief'
                          AND user_id = (SELECT id FROM users WHERE email = :email)
                    """),
                    {"email": email},
                )
                await session.commit()
                logger.warning("Spam complaint from %s — daily-brief entitlement expired", email)

    return JSONResponse({"ok": True})


class CrawlRequest(BaseModel):
    since: str | None = None  # ISO date, e.g. "2026-05-01"; defaults to last crawled date


@app.post("/crawl/puct")
async def trigger_crawl_puct(body: CrawlRequest | None = None) -> JSONResponse:
    if body is None:
        body = CrawlRequest()
    job_id = await enqueue("crawl-puct", {"since": body.since}, priority=10)
    logger.info("Enqueued crawl-puct job %s (since=%s)", job_id, body.since)
    return JSONResponse({"job_id": job_id, "status": "queued"})


@app.post("/crawl/ercot")
async def trigger_crawl_ercot(body: CrawlRequest | None = None) -> JSONResponse:
    if body is None:
        body = CrawlRequest()
    job_id = await enqueue("crawl-ercot", {"since": body.since}, priority=10)
    logger.info("Enqueued crawl-ercot job %s (since=%s)", job_id, body.since)
    return JSONResponse({"job_id": job_id, "status": "queued"})



class BriefTriggerRequest(BaseModel):
    brief_date: str | None = None  # ISO date; defaults to today


@app.post("/brief/trigger")
async def trigger_brief(body: BriefTriggerRequest | None = None) -> JSONResponse:
    """Enqueue compose-brief jobs for all active users for the given date (default: today).

    Skips users already enqueued for that date, so safe to call multiple times.
    """
    if body is None:
        body = BriefTriggerRequest()
    target_date = date.fromisoformat(body.brief_date) if body.brief_date else date.today()

    user_ids = await get_active_user_ids()
    already = await get_already_enqueued_for_date(target_date)
    enqueued = []
    skipped = []
    for uid in user_ids:
        if uid in already:
            skipped.append(str(uid))
            continue
        await enqueue(
            "compose-brief",
            {"user_id": uid, "brief_date": target_date.isoformat()},
            priority=5,
        )
        enqueued.append(str(uid))

    logger.info(
        "brief/trigger: date=%s enqueued=%d skipped=%d",
        target_date, len(enqueued), len(skipped),
    )
    return JSONResponse({
        "brief_date": target_date.isoformat(),
        "enqueued": len(enqueued),
        "skipped": len(skipped),
    })


class RecomposeRequest(BaseModel):
    user_id: str        # UUID string
    brief_date: str     # ISO date, e.g. "2026-05-12"
    idempotency_key: str


@app.post("/brief/recompose", dependencies=[Depends(verify_bearer)])
async def recompose_brief(body: RecomposeRequest) -> JSONResponse:
    """Enqueue a compose-brief job for a single user (admin action).

    Protected by bearer token. Idempotent — repeated calls with the same
    idempotency_key return the original job_id with status "already_queued".
    """
    if not await get_user_exists(body.user_id):
        return JSONResponse({"error": "user not found"}, status_code=404)

    job_id, created = await enqueue_idempotent(
        "compose-brief",
        {"user_id": body.user_id, "brief_date": body.brief_date},
        idempotency_key=body.idempotency_key,
        priority=10,
    )
    logger.info(
        "brief/recompose: user=%s date=%s job=%s created=%s",
        body.user_id, body.brief_date, job_id, created,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        {"job_id": job_id, "status": "queued" if created else "already_queued"},
        status_code=status_code,
    )


class RefreshExtractionRequest(BaseModel):
    filing_id: str      # UUID string
    idempotency_key: str


@app.post("/extraction/refresh", dependencies=[Depends(verify_bearer)])
async def refresh_extraction(body: RefreshExtractionRequest) -> JSONResponse:
    """Enqueue a refresh-extraction job for a single filing (admin action).

    Protected by bearer token. The handler fetches r2_key and doc_type from the
    DB itself — the caller only needs the filing_id. Idempotent via idempotency_key.
    """
    if not await get_filing(body.filing_id):
        return JSONResponse({"error": "filing not found"}, status_code=404)

    job_id, created = await enqueue_idempotent(
        "refresh-extraction",
        {"filing_id": body.filing_id},
        idempotency_key=body.idempotency_key,
        priority=10,
    )
    logger.info(
        "extraction/refresh: filing=%s job=%s created=%s",
        body.filing_id, job_id, created,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        {"job_id": job_id, "status": "queued" if created else "already_queued"},
        status_code=status_code,
    )


class RefreshDocketRequest(BaseModel):
    docket_number: str
    user_id: str         # advisory — bearer token is the security boundary
    max_filings: int = _REFRESH_DOCKET_MAX_FILINGS


@app.post("/extraction/refresh-docket", dependencies=[Depends(verify_bearer)])
async def refresh_docket(body: RefreshDocketRequest) -> JSONResponse:
    """Enqueue refresh-extraction jobs for un-extracted filings in a docket.

    Called by trackDocket in nodalpulse-web when a user pins a docket.
    Rate-limited to REFRESH_DOCKET_USER_HOURLY_CAP jobs per user per hour.
    Returns {docket_number, queued, already_extracted}.
    """
    max_f = min(body.max_filings, _REFRESH_DOCKET_MAX_FILINGS)

    async with AsyncSessionLocal() as session:
        rate_result = await session.execute(
            text("""
                SELECT COUNT(*) FROM jobs
                WHERE kind = 'refresh-extraction'
                  AND payload->>'user_id' = :user_id
                  AND created_at >= NOW() - INTERVAL '1 hour'
            """),
            {"user_id": body.user_id},
        )
        recent = int(rate_result.scalar_one())

        if recent >= _REFRESH_DOCKET_HOURLY_CAP:
            logger.warning(
                "refresh-docket rate limit hit: user=%s docket=%s queued_last_hour=%d",
                body.user_id, body.docket_number, recent,
            )
            return JSONResponse(
                {"error": "rate_limit_exceeded", "queued_last_hour": recent},
                status_code=429,
            )

        effective_max = min(max_f, _REFRESH_DOCKET_HOURLY_CAP - recent)

        filings_result = await session.execute(
            text("""
                SELECT f.id::text AS filing_id, f.r2_key, f.doc_type,
                       (e.id IS NOT NULL) AS already_extracted
                FROM filings f
                LEFT JOIN extractions e ON e.filing_id = f.id
                WHERE f.docket_id = (
                    SELECT id FROM dockets
                    WHERE external_id = :docket_number
                      AND source_id = CAST(:source_id AS uuid)
                )
                ORDER BY f.filed_at DESC
                LIMIT :limit
            """),
            {
                "docket_number": body.docket_number,
                "source_id": _PUCT_SOURCE_ID,
                "limit": effective_max + 50,
            },
        )
        rows = filings_result.mappings().all()

    queued = 0
    already_extracted = 0
    for row in rows:
        if row["already_extracted"]:
            already_extracted += 1
            continue
        if queued >= effective_max:
            break
        await enqueue(
            "refresh-extraction",
            {"filing_id": row["filing_id"], "user_id": body.user_id},
            priority=8,
        )
        queued += 1

    logger.info(
        "refresh-docket user=%s docket=%s found=%d already_extracted=%d enqueued=%d",
        body.user_id, body.docket_number, len(rows), already_extracted, queued,
    )
    return JSONResponse({
        "docket_number": body.docket_number,
        "queued": queued,
        "already_extracted": already_extracted,
    })


# ── admin: job inspection and purge ──────────────────────────────────────────

@app.get("/admin/jobs", dependencies=[Depends(verify_bearer)])
async def admin_jobs_inspect(kind: str = "extract", status: str = "pending") -> JSONResponse:
    """Count jobs matching kind + status.

    ?kind=extract&status=pending (defaults)
    Use status=running to inspect zombie jobs.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM jobs WHERE kind = :kind AND status = :status"),
            {"kind": kind, "status": status},
        )
    return JSONResponse({"kind": kind, "status": status, "count": int(result.scalar_one())})


class PurgeJobsRequest(BaseModel):
    kind: str
    status: str = "pending"


@app.post("/admin/jobs/purge", dependencies=[Depends(verify_bearer)])
async def admin_jobs_purge(body: PurgeJobsRequest) -> JSONResponse:
    """Mark jobs as failed so they stop blocking the queue.

    For status='running', only matches jobs whose locked_until has expired
    (updated_at < NOW() - 1h) — safe to call while the worker is live without
    risking in-flight jobs.
    """
    async with AsyncSessionLocal() as session:
        if body.status == "running":
            result = await session.execute(
                text("""
                    UPDATE jobs
                    SET status = 'failed',
                        error = 'purged by admin (zombie running job)',
                        locked_by = NULL,
                        locked_until = NULL,
                        updated_at = NOW()
                    WHERE kind = :kind
                      AND status = 'running'
                      AND updated_at < NOW() - INTERVAL '1 hour'
                """),
                {"kind": body.kind},
            )
        else:
            result = await session.execute(
                text("""
                    UPDATE jobs
                    SET status = 'failed',
                        error = 'purged by admin',
                        updated_at = NOW()
                    WHERE kind = :kind AND status = :status
                """),
                {"kind": body.kind, "status": body.status},
            )
        purged = result.rowcount
        await session.commit()
    logger.info("admin/jobs/purge: kind=%s status=%s purged=%d", body.kind, body.status, purged)
    return JSONResponse({"kind": body.kind, "status": body.status, "purged": purged})


# ── admin: LLM cost aggregations ─────────────────────────────────────────────

@app.get("/admin/llm-costs", dependencies=[Depends(verify_bearer)])
async def llm_costs(days: int = 7) -> JSONResponse:
    """Daily LLM cost aggregations by pipeline stage and model.

    ?days=N caps the window to N days back from now (1–90, default 7).
    """
    days = max(1, min(days, 90))

    async with AsyncSessionLocal() as session:
        by_day_result = await session.execute(
            text("""
                SELECT
                    date_trunc('day', created_at)::date            AS day,
                    pipeline_stage                                  AS stage,
                    model,
                    pricing_version,
                    COUNT(*)::int                                   AS calls,
                    COALESCE(SUM(input_tokens), 0)::int             AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)::int            AS output_tokens,
                    COALESCE(SUM(cache_read_input_tokens), 0)::int  AS cache_read_input_tokens,
                    ROUND(COALESCE(SUM(cost_usd_estimate), 0), 6)   AS cost_usd
                FROM llm_calls
                WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
                GROUP BY 1, 2, 3, 4
                ORDER BY 1 DESC, 2, 3
            """),
            {"days": days},
        )
        totals_result = await session.execute(
            text("""
                SELECT
                    COUNT(*)::int                                    AS calls,
                    ROUND(COALESCE(SUM(cost_usd_estimate), 0), 6)   AS cost_usd
                FROM llm_calls
                WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
            """),
            {"days": days},
        )

    rows = by_day_result.mappings().all()
    totals_row = totals_result.mappings().first()

    return JSONResponse({
        "range": {
            "from": (date.today() - timedelta(days=days - 1)).isoformat(),
            "to": date.today().isoformat(),
        },
        "totals": {
            "cost_usd": float(totals_row["cost_usd"] or 0) if totals_row else 0.0,
            "calls": int(totals_row["calls"] or 0) if totals_row else 0,
        },
        "by_day": [
            {
                "day": str(row["day"]),
                "stage": row["stage"],
                "model": row["model"],
                "pricing_version": row["pricing_version"],
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_input_tokens": row["cache_read_input_tokens"],
                "cost_usd": float(row["cost_usd"] or 0),
            }
            for row in rows
        ],
    })


# ── admin: PUCT lead scraper ──────────────────────────────────────────────────

_scrape_jobs: dict[str, dict] = {}


async def _run_scrape(job_id: str, dockets: str) -> None:
    out_path = f"/tmp/leads-{job_id}.csv"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/scrape_puct_commenters.py",
            "--dockets", dockets, "--out", out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/app",
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        if proc.returncode != 0:
            _scrape_jobs[job_id] = {"status": "error", "csv": None, "error": stderr.decode()}
            return
        _scrape_jobs[job_id] = {
            "status": "done",
            "csv": Path(out_path).read_text(encoding="utf-8"),
            "error": None,
        }
    except Exception as exc:
        _scrape_jobs[job_id] = {"status": "error", "csv": None, "error": str(exc)}


@app.post("/admin/scrape-leads", dependencies=[Depends(verify_bearer)])
async def start_scrape_leads(dockets: str = "59475,58923,59336") -> JSONResponse:
    """Start a background PUCT lead scrape. Poll GET /admin/scrape-leads/{job_id} for results."""
    job_id = uuid.uuid4().hex[:8]
    _scrape_jobs[job_id] = {"status": "running", "csv": None, "error": None}
    asyncio.create_task(_run_scrape(job_id, dockets))
    logger.info("scrape-leads job %s started for dockets=%s", job_id, dockets)
    return JSONResponse({"job_id": job_id, "status": "running"})


@app.get("/admin/scrape-leads/{job_id}", dependencies=[Depends(verify_bearer)])
async def get_scrape_leads(job_id: str):
    """Poll scrape job status. Returns CSV when done."""
    job = _scrape_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    if job["status"] == "done":
        return Response(
            content=(job["csv"] or "").encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=leads-{date.today().isoformat()}.csv"},
        )
    return JSONResponse({"status": job["status"], "error": job["error"]})


@app.get("/admin/top-dockets", dependencies=[Depends(verify_bearer)])
async def top_dockets(min_filers: int = 3, limit: int = 30) -> JSONResponse:
    """Return dockets ranked by unique-filer count — use to find professional-filer dockets."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT d.external_id, d.title, COUNT(DISTINCT f.filer) AS uniq_filers
                FROM filings f
                JOIN dockets d ON d.id = f.docket_id
                WHERE f.filer IS NOT NULL AND f.filer != ''
                GROUP BY d.external_id, d.title
                HAVING COUNT(DISTINCT f.filer) >= :min_filers
                ORDER BY uniq_filers DESC
                LIMIT :limit
            """),
            {"min_filers": min_filers, "limit": limit},
        )
        rows = result.mappings().all()
    return JSONResponse([
        {"docket": r["external_id"], "title": r["title"], "uniq_filers": r["uniq_filers"]}
        for r in rows
    ])
