import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import text

from nodalpulse.api.auth import verify_bearer
from nodalpulse.api.crawl_probes import probe_cpuc, probe_ferc, probe_puct
from nodalpulse.api.qna import QnaRequest, handle_qna
from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date, get_user_exists
from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.db.extractions import get_filing
from nodalpulse.db.filings import find_or_create_docket, get_source_id
from nodalpulse.queue.pg_queue import enqueue, enqueue_idempotent
from nodalpulse.saved_search_predicate import build_predicate_bundle
from nodalpulse.settings import settings
from nodalpulse.zone_lookup import ilike_patterns_for_zones

logger = logging.getLogger(__name__)
app = FastAPI(title="nodalpulse-services", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nodalpulse.com", "https://www.nodalpulse.com"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

_REFRESH_DOCKET_HOURLY_CAP = int(os.environ.get("REFRESH_DOCKET_USER_HOURLY_CAP", "30"))
_REFRESH_DOCKET_MAX_FILINGS = int(os.environ.get("REFRESH_DOCKET_MAX_FILINGS_PER_PIN", "15"))

# TODO: support ERCOT source via explicit source param once ERCOT docket tracking ships
_PUCT_SOURCE_ID = "0725032a-239f-475d-bdd5-251adad3ae05"


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── public record endpoints (no auth, 0 LLM calls, whitelisted fields only) ──

_VALID_MARKETS = frozenset(
    {"puct", "caiso", "ferc", "pjm", "ercot-nprr", "ercot-mn", "cpuc", "vascc", "mdpsc", "njbpu"}
)

# How many extracted filings the PUBLIC (crawlable) record teaser renders. Was 3,
# which left record pages thin — Google marked ~half "crawled, currently not
# indexed". The full list of titles/dockets/summaries is public record metadata;
# only the structured analysis (all deadlines/parties/$) stays gated behind the
# lead form (see /public/record/depth). Tune here.
_TEASER_ITEM_LIMIT = 50

# In-memory rate limiter for /public/lead: max 5 submissions per IP per 10 min
_lead_ip_log: dict[str, list[float]] = defaultdict(list)
_LEAD_RATE_LIMIT = 5
_LEAD_RATE_WINDOW = 600


def _check_lead_rate(ip: str) -> bool:
    now = time.time()
    _lead_ip_log[ip] = [t for t in _lead_ip_log[ip] if now - t < _LEAD_RATE_WINDOW]
    if len(_lead_ip_log[ip]) >= _LEAD_RATE_LIMIT:
        return False
    _lead_ip_log[ip].append(now)
    return True


# In-memory rate limiter for /public/record-request. Each call probes an external
# source (PUCT/FERC/CPUC), so keep it tighter than the plain lead form: 4 / 10 min.
_record_req_ip_log: dict[str, list[float]] = defaultdict(list)
_RECORD_REQ_RATE_LIMIT = 4
_RECORD_REQ_RATE_WINDOW = 600


def _check_record_req_rate(ip: str) -> bool:
    now = time.time()
    _record_req_ip_log[ip] = [
        t for t in _record_req_ip_log[ip] if now - t < _RECORD_REQ_RATE_WINDOW
    ]
    if len(_record_req_ip_log[ip]) >= _RECORD_REQ_RATE_LIMIT:
        return False
    _record_req_ip_log[ip].append(now)
    return True


def _make_lead_token(email: str, secret: str) -> str:
    """Stateless HMAC token; valid 24 h. No DB lookup needed on verify."""
    expiry = int(time.time()) + 86400
    payload = f"{email}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _verify_lead_token(token: str, secret: str) -> str | None:
    """Returns the email if the token is valid and unexpired, else None."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        # structure: <email>:<expiry_unix>:<sha256_hex>
        # split from right twice so emails containing ':' are handled correctly
        last = decoded.rfind(":")
        sig = decoded[last + 1 :]
        rest = decoded[:last]
        second = rest.rfind(":")
        expiry_str = rest[second + 1 :]
        email = rest[:second]
        payload = f"{email}:{expiry_str}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        if int(expiry_str) < int(time.time()):
            return None
        return email
    except Exception:
        return None


_INDEX_MARKETS = ["puct", "caiso", "ferc", "pjm", "cpuc", "vascc", "mdpsc", "njbpu"]


@app.get("/public/record/index")
async def public_record_index() -> JSONResponse:
    """Build manifest: (market, date) pairs that have ≥1 relevant extraction.

    Used by the marketing site's getStaticPaths at build time.
    Only pairs with real content are returned — avoids thin-content pages and
    ensures URLs persist across rebuilds (corpus compounds, never 404s on old dates).
    Zero LLM calls. No auth.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    s.slug            AS market,
                    f.filed_at::date  AS date,
                    COUNT(e.id)::int  AS item_count
                FROM extractions e
                JOIN filings f ON e.filing_id = f.id
                JOIN sources s ON s.id = f.source_id
                WHERE e.haiku_verdict = 'relevant'
                  AND s.slug = ANY(:markets)
                GROUP BY s.slug, f.filed_at::date
                HAVING COUNT(e.id) >= 1
                ORDER BY date DESC, market
            """),
            {"markets": _INDEX_MARKETS},
        )
        rows = result.mappings().all()

    return JSONResponse(
        [
            {"market": r["market"], "date": str(r["date"]), "item_count": r["item_count"]}
            for r in rows
        ]
    )


@app.get("/public/record")
async def public_record(
    market: str,
    day: str = Query(alias="date"),
) -> JSONResponse:
    """Teaser for a market+date record page.

    Returns filing count (breadth), up to _TEASER_ITEM_LIMIT headline items already
    extracted with haiku_verdict='relevant', and total deadline count across them.
    Safe fields only — no PII, no user tracking, no raw payload dump.
    Zero LLM calls.
    """
    if market not in _VALID_MARKETS:
        return JSONResponse({"error": "unknown market"}, status_code=400)
    try:
        record_date = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "invalid date"}, status_code=400)

    day_start = datetime.combine(record_date, datetime.min.time()).replace(tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    params: dict = {
        "market": market,
        "day_start": day_start,
        "day_end": day_end,
        "teaser_limit": _TEASER_ITEM_LIMIT,
    }

    async with AsyncSessionLocal() as session:
        count_row = await session.execute(
            text("""
                SELECT COUNT(*) FROM filings f
                JOIN sources s ON s.id = f.source_id
                WHERE s.slug = :market
                  AND f.filed_at >= :day_start
                  AND f.filed_at < :day_end
            """),
            params,
        )
        filing_count = int(count_row.scalar_one())

        items_rows = await session.execute(
            text("""
                SELECT
                    f.title,
                    f.source_url,
                    f.filed_at,
                    d.external_id              AS docket_number,
                    e.payload->>'summary'      AS summary,
                    e.payload->'key_points'    AS key_points
                FROM extractions e
                JOIN filings f ON e.filing_id = f.id
                JOIN sources s ON s.id = f.source_id
                LEFT JOIN dockets d ON d.id = f.docket_id
                WHERE s.slug = :market
                  AND f.filed_at >= :day_start
                  AND f.filed_at < :day_end
                  AND e.haiku_verdict = 'relevant'
                ORDER BY f.filed_at DESC
                LIMIT :teaser_limit
            """),
            params,
        )
        item_rows = items_rows.mappings().all()

        deadline_row = await session.execute(
            text("""
                SELECT COALESCE(SUM(jsonb_array_length(e.payload->'deadlines')), 0)
                FROM extractions e
                JOIN filings f ON e.filing_id = f.id
                JOIN sources s ON s.id = f.source_id
                WHERE s.slug = :market
                  AND f.filed_at >= :day_start
                  AND f.filed_at < :day_end
                  AND e.haiku_verdict = 'relevant'
                  AND e.payload ? 'deadlines'
            """),
            params,
        )
        deadline_count = int(deadline_row.scalar_one() or 0)

    items = [
        {
            "title": r["title"],
            "source_url": r["source_url"],
            "filed_at": r["filed_at"].isoformat() if r["filed_at"] else None,
            "docket_number": r["docket_number"],
            "summary": r["summary"],
            "key_points": (r["key_points"] or [])[:2],
        }
        for r in item_rows
    ]

    return JSONResponse(
        {
            "market": market,
            "date": day,
            "filing_count": filing_count,
            "items": items,
            "deadline_count": deadline_count,
        }
    )


class LeadRequest(BaseModel):
    email: str
    name: str
    title: str  # job title / cargo
    market: str | None = None
    record_date: str | None = None
    website: str = ""  # honeypot — bots fill this; humans leave it blank


@app.post("/public/lead")
async def capture_lead(body: LeadRequest, request: Request) -> JSONResponse:
    """Capture email+name+title and return a 24-h HMAC token for /public/record/depth.

    Anti-spam: honeypot field + per-IP rate limit (5 / 10 min).
    The lead is upserted by email — re-submission updates name/title but keeps
    the original captured_at via the UNIQUE constraint semantics.
    """
    if body.website:
        # Honeypot triggered — silently succeed so bots think they won
        return JSONResponse({"ok": True, "token": ""})

    client_ip = request.client.host if request.client else "unknown"
    if not _check_lead_rate(client_ip):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    email = body.email.lower().strip()
    name = body.name.strip()
    title = body.title.strip()

    if "@" not in email or len(email) < 3:
        return JSONResponse({"error": "invalid_email"}, status_code=400)
    if not name:
        return JSONResponse({"error": "name_required"}, status_code=400)
    if not title:
        return JSONResponse({"error": "title_required"}, status_code=400)

    secret = settings.lead_token_secret
    if not secret:
        logger.error("lead_token_secret not configured — /public/lead disabled")
        return JSONResponse({"error": "server_error"}, status_code=500)

    parsed_record_date: date | None = None
    if body.record_date:
        with contextlib.suppress(ValueError):
            parsed_record_date = date.fromisoformat(body.record_date)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO leads (email, name, title, market, record_date)
                VALUES (:email, :name, :title, :market, :record_date)
                ON CONFLICT (email) DO UPDATE
                    SET name        = EXCLUDED.name,
                        title       = EXCLUDED.title,
                        market      = COALESCE(EXCLUDED.market, leads.market),
                        record_date = COALESCE(EXCLUDED.record_date, leads.record_date)
            """),
            {
                "email": email,
                "name": name,
                "title": title,
                "market": body.market,
                "record_date": parsed_record_date,
            },
        )
        await session.commit()

    token = _make_lead_token(email, secret)
    logger.info("lead captured: email=%s market=%s", email, body.market)
    return JSONResponse({"ok": True, "token": token})


class RecordRequestBody(BaseModel):
    email: str
    market: str  # "puct" | "ferc" | "cpuc"
    docket: str  # control number / proceeding id, e.g. "58481", "EL25-49"
    website: str = ""  # honeypot — bots fill this; humans leave it blank


# Markets the "check a docket" lead magnet can probe (must have a probe_* fn below).
_RECORD_REQUEST_MARKETS = frozenset({"puct", "ferc", "cpuc"})
_RECORD_REQUEST_LABELS = {"puct": "PUCT", "ferc": "FERC", "cpuc": "CPUC"}


@app.post("/public/record-request")
async def public_record_request(body: RecordRequestBody, request: Request) -> JSONResponse:
    """Lead magnet: a visitor drops a docket number; we confirm it exists in the
    primary source, capture the lead, and return the filing count so the site can
    invite them into a free trial where the full Record assembles. This is the
    product-led replacement for the retired /digest email capture.

    Public (CORS-locked to nodalpulse.com). Honeypot + per-IP rate limit.

    Deliberately does NOT enqueue a crawl from this anonymous endpoint: the probe
    confirms the docket cheaply, and the billable assembly (crawl + selective
    extraction) runs only when a signed-in user tracks the docket on trial, via the
    existing /crawl/on-demand wiring. This keeps an unauthenticated public endpoint
    from triggering paid-compute work.
    """
    if body.website:
        # Honeypot triggered — silently succeed so bots think they won.
        return JSONResponse({"ok": True, "found": 0, "valid": False})

    client_ip = request.client.host if request.client else "unknown"
    if not _check_record_req_rate(client_ip):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    market = body.market.lower().strip()
    docket = body.docket.strip()
    email = body.email.lower().strip()

    if market not in _RECORD_REQUEST_MARKETS:
        return JSONResponse({"error": "unsupported_market"}, status_code=400)
    if not docket or len(docket) > 40:
        return JSONResponse({"error": "invalid_docket"}, status_code=400)
    if "@" not in email or len(email) < 3:
        return JSONResponse({"error": "invalid_email"}, status_code=400)

    # Inline probe (~1–3 s) — confirm the docket has ≥1 filing in the primary source.
    if market == "cpuc":
        found = await probe_cpuc(docket)
    elif market == "puct":
        found = await probe_puct(docket)
    else:  # ferc
        found = await probe_ferc(docket)

    if not found:
        logger.info("record-request: not found market=%s docket=%s", market, docket)
        return JSONResponse({"found": 0, "valid": False})

    # Capture the lead + which docket they asked about. This magnet is
    # intentionally low-friction (docket + email only), but leads.name/title are
    # NOT NULL, so we write self-labeling placeholders: name marks the capture
    # channel and title carries the requested docket for the admin list;
    # source_url holds the machine-readable ref. Upsert by email, and on conflict
    # update ONLY market + source_url so a richer name/title captured via the
    # record-page gate is never clobbered.
    label = _RECORD_REQUEST_LABELS[market]
    docket_ref = f"docket-request:{market}:{docket}"
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO leads (email, name, title, market, source_url)
                VALUES (:email, :name, :title, :market, :source_url)
                ON CONFLICT (email) DO UPDATE
                    SET market     = COALESCE(EXCLUDED.market, leads.market),
                        source_url = EXCLUDED.source_url
            """),
            {
                "email": email,
                "name": "Docket-check lead",
                "title": f"{label} {docket}",
                "market": label,
                "source_url": docket_ref,
            },
        )
        await session.commit()

    logger.info(
        "record-request captured: email=%s market=%s docket=%s found=%d",
        email,
        market,
        docket,
        found,
    )
    return JSONResponse({"found": found, "valid": True, "market": market, "docket": docket})


@app.get("/public/record/depth")
async def public_record_depth(
    market: str,
    day: str = Query(alias="date"),
    token: str = "",
) -> JSONResponse:
    """Gated depth for a market+date. Requires a valid lead token from /public/lead.

    Returns all deadlines/parties/$ for already-extracted relevant filings.
    Whitelisted payload fields only — no raw payload dump, no user PII.
    Zero LLM calls. Returns 401 without a valid token (cannot be scraped).
    """
    secret = settings.lead_token_secret
    if not secret:
        return JSONResponse({"error": "server_error"}, status_code=500)

    email = _verify_lead_token(token, secret)
    if not email:
        return JSONResponse({"error": "token_required"}, status_code=401)

    if market not in _VALID_MARKETS:
        return JSONResponse({"error": "unknown market"}, status_code=400)
    try:
        record_date = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "invalid date"}, status_code=400)

    day_start = datetime.combine(record_date, datetime.min.time()).replace(tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    async with AsyncSessionLocal() as session:
        rows_result = await session.execute(
            text("""
                SELECT
                    f.title,
                    f.source_url,
                    f.filed_at,
                    f.filer,
                    d.external_id                   AS docket_number,
                    e.payload->>'summary'            AS summary,
                    e.payload->>'relief_requested'   AS relief_requested,
                    e.payload->>'outcome'            AS outcome,
                    e.payload->>'effective_date'     AS effective_date,
                    e.payload->'key_points'          AS key_points,
                    e.payload->'deadlines'           AS deadlines
                FROM extractions e
                JOIN filings f ON e.filing_id = f.id
                JOIN sources s ON s.id = f.source_id
                LEFT JOIN dockets d ON d.id = f.docket_id
                WHERE s.slug = :market
                  AND f.filed_at >= :day_start
                  AND f.filed_at < :day_end
                  AND e.haiku_verdict = 'relevant'
                ORDER BY f.filed_at DESC
            """),
            {"market": market, "day_start": day_start, "day_end": day_end},
        )
        rows = rows_result.mappings().all()

    items = [
        {
            "title": r["title"],
            "source_url": r["source_url"],
            "filed_at": r["filed_at"].isoformat() if r["filed_at"] else None,
            "filer": r["filer"],
            "docket_number": r["docket_number"],
            "summary": r["summary"],
            "relief_requested": r["relief_requested"],
            "outcome": r["outcome"],
            "effective_date": r["effective_date"],
            "key_points": r["key_points"] or [],
            "deadlines": r["deadlines"] or [],
        }
        for r in rows
    ]

    return JSONResponse({"market": market, "date": day, "items": items})


# ── email endpoints ───────────────────────────────────────────────────────────


@app.get("/unsubscribe/{user_id}", response_class=HTMLResponse)
async def unsubscribe_get(user_id: str) -> HTMLResponse:
    """One-click unsubscribe landing page (GET renders a confirmation form)."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Unsubscribe — NodalPulse</title>
<style>body{font-family:sans-serif;max-width:480px;margin:80px auto;padding:0 16px;color:#44403C}</style>
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
                WHERE user_id = CAST(:uid AS uuid) AND feature = 'daily_brief'
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
                        WHERE feature = 'daily_brief'
                          AND user_id = (SELECT id FROM users WHERE email = :email)
                    """),
                    {"email": email},
                )
                await session.commit()
                logger.warning("Hard bounce for %s — daily_brief entitlement expired", email)

            elif event_type == "complaint":
                await session.execute(
                    text("""
                        UPDATE entitlements SET expires_at = NOW()
                        WHERE feature = 'daily_brief'
                          AND user_id = (SELECT id FROM users WHERE email = :email)
                    """),
                    {"email": email},
                )
                await session.commit()
                logger.warning("Spam complaint from %s — daily_brief entitlement expired", email)

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
        target_date,
        len(enqueued),
        len(skipped),
    )
    return JSONResponse(
        {
            "brief_date": target_date.isoformat(),
            "enqueued": len(enqueued),
            "skipped": len(skipped),
        }
    )


class RecomposeRequest(BaseModel):
    user_id: str  # UUID string
    brief_date: str  # ISO date, e.g. "2026-05-12"
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
        body.user_id,
        body.brief_date,
        job_id,
        created,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        {"job_id": job_id, "status": "queued" if created else "already_queued"},
        status_code=status_code,
    )


class RefreshExtractionRequest(BaseModel):
    filing_id: str  # UUID string
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
        body.filing_id,
        job_id,
        created,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        {"job_id": job_id, "status": "queued" if created else "already_queued"},
        status_code=status_code,
    )


class FireSavedSearchRequest(BaseModel):
    user_id: str  # UUID string
    saved_search_id: str  # UUID string


_FIRE_WINDOW_DAYS = 30
_FIRE_MAX_RESULTS = 25


@app.post("/saved-search/fire", dependencies=[Depends(verify_bearer)])
async def fire_saved_search(body: FireSavedSearchRequest) -> JSONResponse:
    """Run a single saved search against recent filings and return matches.

    Windows on filings.created_at (crawl time) for the last 30 days.
    Returns up to 25 filings ordered by
    filed_at desc. Does NOT write last_fired_at — the web layer handles that.
    """
    async with AsyncSessionLocal() as session:
        # Verify saved search exists and belongs to the user
        ss_result = await session.execute(
            text("""
                SELECT id::text AS id, name, query
                FROM saved_searches
                WHERE id = CAST(:ss_id AS uuid)
                  AND user_id = CAST(:uid AS uuid)
            """),
            {"ss_id": body.saved_search_id, "uid": body.user_id},
        )
        ss_row = ss_result.mappings().first()
        if not ss_row:
            return JSONResponse({"error": "saved search not found"}, status_code=404)

        # Load user's tracked dockets (merge profile array + junction table)
        profile_result = await session.execute(
            text("""
                SELECT
                    COALESCE(tracked_docket_ids::text[], '{}') AS profile_ids,
                    COALESCE(tracked_tags, '[]'::json)         AS tracked_tags,
                    COALESCE(market_roles, '{}')               AS market_roles
                FROM user_profiles
                WHERE user_id = CAST(:uid AS uuid)
            """),
            {"uid": body.user_id},
        )
        profile = profile_result.mappings().first()
        profile_docket_ids: list[str] = list(profile["profile_ids"]) if profile else []
        tracked_tags: list[str] = list(profile["tracked_tags"]) if profile else []
        market_roles: list[str] = list(profile["market_roles"]) if profile else []

        junc_result = await session.execute(
            text("SELECT docket_id::text FROM user_dockets WHERE user_id = CAST(:uid AS uuid)"),
            {"uid": body.user_id},
        )
        junction_ids = [r[0] for r in junc_result.fetchall() if r[0]]
        tracked_docket_uuids = list({*profile_docket_ids, *junction_ids})

    zone_patterns = ilike_patterns_for_zones(tracked_tags)

    bundle = build_predicate_bundle(
        saved_searches=[{"id": str(ss_row["id"]), "query": ss_row["query"]}],
        tracked_docket_uuids=tracked_docket_uuids,
        zone_filer_patterns=zone_patterns,
        market_roles=market_roles,
    )

    if not bundle.has_implementable_predicates:
        return JSONResponse(
            {
                "saved_search_id": body.saved_search_id,
                "filing_count": 0,
                "filings": [],
            }
        )

    until = datetime.now(UTC)
    since = until - timedelta(days=_FIRE_WINDOW_DAYS)
    where_clause, params = bundle.build_where_clause()
    params["since"] = since
    params["until"] = until
    params["limit"] = _FIRE_MAX_RESULTS

    sql_query = f"""
        SELECT
            f.id::text   AS id,
            f.title,
            s.slug       AS source_slug,
            f.filed_at,
            f.source_url AS url
        FROM filings f
        JOIN sources s ON s.id = f.source_id
        LEFT JOIN extractions e ON e.filing_id = f.id
        WHERE f.created_at >= :since
          AND f.created_at < :until
          AND e.haiku_verdict IS DISTINCT FROM 'irrelevant'
          AND ({where_clause})
        ORDER BY f.filed_at DESC
        LIMIT :limit
    """  # noqa: S608 — no user input in this string; all values are bound params

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql_query), params)
        rows = result.mappings().fetchall()

    filings = [
        {
            "id": r["id"],
            "title": r["title"],
            "source_slug": r["source_slug"],
            "filed_at": r["filed_at"].isoformat() if r["filed_at"] else None,
            "url": r["url"],
        }
        for r in rows
    ]

    logger.info(
        "saved-search/fire: user=%s search=%s found=%d",
        body.user_id,
        body.saved_search_id,
        len(filings),
    )
    return JSONResponse(
        {
            "saved_search_id": body.saved_search_id,
            "filing_count": len(filings),
            "filings": filings,
        }
    )


class RefreshDocketRequest(BaseModel):
    docket_number: str
    user_id: str  # advisory — bearer token is the security boundary
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
                body.user_id,
                body.docket_number,
                recent,
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
        body.user_id,
        body.docket_number,
        len(rows),
        already_extracted,
        queued,
    )
    return JSONResponse(
        {
            "docket_number": body.docket_number,
            "queued": queued,
            "already_extracted": already_extracted,
        }
    )


# ── On-demand crawl ───────────────────────────────────────────────────────────

_ON_DEMAND_MAX_FILINGS = int(os.environ.get("ON_DEMAND_MAX_FILINGS", "30"))
_ON_DEMAND_SINCE = os.environ.get("ON_DEMAND_SINCE", "2020-01-01")
_ON_DEMAND_RATE_CAP = int(os.environ.get("ON_DEMAND_USER_HOURLY_CAP", "3"))
_ON_DEMAND_ALLOWED = frozenset({"cpuc", "ferc", "puct"})


class OnDemandCrawlRequest(BaseModel):
    source_slug: str  # "cpuc" | "ferc" | "puct"
    proceeding_id: str  # e.g. "A2508008", "EL25-49", "58481" (PUCT control number)
    user_id: str  # advisory — bearer token is the security boundary


@app.post("/crawl/on-demand", dependencies=[Depends(verify_bearer)])
async def crawl_on_demand(body: OnDemandCrawlRequest) -> JSONResponse:
    """Validate a non-PUCT proceeding exists, then enqueue a capped backfill crawl.

    Called by trackDocket in nodalpulse-web when a user tracks a CPUC or FERC/PJM
    docket that is not yet in the index.

    Step 1 (inline, ~1-3 s): probe the source to confirm ≥1 filing exists.
      Returns {found: 0} immediately if the proceeding is unknown — no stub created.
    Step 2 (async): enqueue a parametrized crawl-cpuc / crawl-ferc job capped to
      ON_DEMAND_BACKFILL_DAYS days of history. run_adapter handles deferred R2,
      selective extraction, and the spending cap via the worker's normal flow.

    Rate-limited to ON_DEMAND_RATE_CAP enqueues per user per hour.
    Returns {found: int, valid: bool, docket_id?: str}.
    """
    if body.source_slug not in _ON_DEMAND_ALLOWED:
        return JSONResponse({"error": "unsupported_source"}, status_code=400)

    # Rate-limit: max _ON_DEMAND_RATE_CAP on-demand enqueues per user per hour
    async with AsyncSessionLocal() as db_session:
        rate_result = await db_session.execute(
            text("""
                SELECT COUNT(*) FROM jobs
                WHERE kind IN ('crawl-cpuc', 'crawl-ferc', 'crawl-puct')
                  AND payload->>'user_id' = :user_id
                  AND created_at >= NOW() - INTERVAL '1 hour'
            """),
            {"user_id": body.user_id},
        )
        recent = int(rate_result.scalar_one())

    if recent >= _ON_DEMAND_RATE_CAP:
        logger.warning(
            "crawl-on-demand rate limit: user=%s source=%s proceeding=%s recent_hour=%d",
            body.user_id,
            body.source_slug,
            body.proceeding_id,
            recent,
        )
        return JSONResponse({"error": "rate_limit_exceeded", "retry_after": 3600}, status_code=429)

    # Step 1 — inline probe: confirm the proceeding has filings in the source
    if body.source_slug == "cpuc":
        found = await probe_cpuc(body.proceeding_id)
    elif body.source_slug == "puct":
        found = await probe_puct(body.proceeding_id)
    else:  # ferc
        found = await probe_ferc(body.proceeding_id)

    if not found:
        logger.info(
            "crawl-on-demand: not found — source=%s proceeding=%s user=%s",
            body.source_slug,
            body.proceeding_id,
            body.user_id,
        )
        return JSONResponse({"found": 0, "valid": False})

    # Step 2 — find-or-create the docket row so the web can link user_dockets to it
    source_id = await get_source_id(body.source_slug)
    if not source_id:
        logger.error("crawl-on-demand: source '%s' not found in sources table", body.source_slug)
        return JSONResponse({"error": "source_not_configured"}, status_code=500)

    jurisdiction = {"cpuc": "CPUC", "ferc": "FERC", "puct": "PUCT"}[body.source_slug]
    docket_id = await find_or_create_docket(source_id, body.proceeding_id, jurisdiction)

    # Step 3 — enqueue parametrized backfill.
    # Governor: 1 proceeding, no date floor (recent-first), capped at ON_DEMAND_MAX_FILINGS.
    # ON_DEMAND_SINCE = "2020-01-01" is the effective floor to bound HTTP call depth while
    # reaching well beyond MAX_LOOKBACK_DAYS; run_adapter bypasses the lookback cap when
    # max_filings is set so this date is respected as-is.
    job_kind = f"crawl-{body.source_slug}"
    _ref_key = {"cpuc": "proc_numbers", "ferc": "docket_numbers", "puct": "control_numbers"}[
        body.source_slug
    ]
    job_payload = {
        _ref_key: [body.proceeding_id],
        "since": _ON_DEMAND_SINCE,
        "max_filings": _ON_DEMAND_MAX_FILINGS,
        "user_id": body.user_id,
    }

    await enqueue(job_kind, job_payload, priority=7)

    logger.info(
        "crawl-on-demand: enqueued %s for proceeding=%s user=%s found=%d docket_id=%s",
        job_kind,
        body.proceeding_id,
        body.user_id,
        found,
        docket_id,
    )
    return JSONResponse({"found": found, "valid": True, "docket_id": docket_id})


# ── Q&A ───────────────────────────────────────────────────────────────────────


@app.post("/qna", dependencies=[Depends(verify_bearer)])
async def qna(body: QnaRequest) -> JSONResponse:
    """Answer a question about the user's tracked filings.

    Rate-limited by limit_per_day (passed by the web layer from entitlements).
    Scopes retrieval to the user's predicate bundle. Uses structured extraction
    payload only — no R2 text retrieval (V1).
    """
    return await handle_qna(body)


# ── Q&A usage ─────────────────────────────────────────────────────────────────


@app.get("/qna/usage", dependencies=[Depends(verify_bearer)])
async def qna_usage(user_id: str) -> JSONResponse:
    """Return today's Q&A question count for a user (America/Chicago day window)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COUNT(*) FROM llm_calls
                WHERE pipeline_stage = 'qna'
                  AND user_id = CAST(:uid AS uuid)
                  AND created_at >= date_trunc('day', now() AT TIME ZONE 'America/Chicago')
                    AT TIME ZONE 'America/Chicago'
            """),
            {"uid": user_id},
        )
        count = int(result.scalar_one())
    return JSONResponse({"user_id": user_id, "used_today": count})


# ── brief history export ──────────────────────────────────────────────────────


class BriefHistoryExportRequest(BaseModel):
    user_id: str
    user_email: str


@app.post("/brief-history/export", dependencies=[Depends(verify_bearer)])
async def brief_history_export(body: BriefHistoryExportRequest) -> JSONResponse:
    """Enqueue a brief-history-export job for an Org-tier user.

    The worker fetches all sent briefs from R2, packs them into a zip archive,
    and emails a presigned download link to user_email.
    """
    job_id = await enqueue(
        "brief-history-export",
        {"user_id": body.user_id, "user_email": body.user_email},
        priority=1,
    )
    return JSONResponse({"queued": True, "job_id": job_id})


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

    return JSONResponse(
        {
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
        }
    )


# ── admin: PUCT lead scraper ──────────────────────────────────────────────────

_scrape_jobs: dict[str, dict] = {}


async def _run_scrape(job_id: str, dockets: str) -> None:
    out_path = f"/tmp/leads-{job_id}.csv"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "scripts/scrape_puct_commenters.py",
            "--dockets",
            dockets,
            "--out",
            out_path,
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
            headers={
                "Content-Disposition": f"attachment; filename=leads-{date.today().isoformat()}.csv"
            },
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
    return JSONResponse(
        [
            {"docket": r["external_id"], "title": r["title"], "uniq_filers": r["uniq_filers"]}
            for r in rows
        ]
    )
