import logging
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date
from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.queue.pg_queue import enqueue

logger = logging.getLogger(__name__)
app = FastAPI(title="nodalpulse-services", version="0.1.0")


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


@app.get("/discover/ercot")
async def discover_ercot_urls() -> JSONResponse:
    """Probe candidate ERCOT URLs with Playwright and return page structure info."""
    from playwright.async_api import async_playwright

    _BROWSER_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    _UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

    results = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()

        # MN row HTML probe — navigate, then dump first 4 data row outerHTML
        mn_url = "https://www.ercot.com/services/comm/mkt_notices/notices"
        try:
            await page.goto(mn_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await page.wait_for_selector("table tr td", timeout=30_000)
            mn_info = await page.evaluate("""() => {
                const tables = Array.from(document.querySelectorAll('table'));
                const dataTable = tables.reduce((a, b) =>
                    b.querySelectorAll('tr').length > a.querySelectorAll('tr').length ? b : a, tables[0]);
                const rows = Array.from(dataTable.querySelectorAll('tr')).slice(0, 5);
                return rows.map(tr => ({
                    outerHTML: tr.outerHTML.slice(0, 1200),
                    cells_text: Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim().slice(0, 80)),
                    has_anchor: !!tr.querySelector('a[href]'),
                    anchor_href: tr.querySelector('a[href]') ? tr.querySelector('a[href]').getAttribute('href') : null,
                    tr_attrs: {
                        onclick: tr.getAttribute('onclick'),
                        ng_click: tr.getAttribute('ng-click'),
                        data_id: tr.getAttribute('data-id'),
                        data_href: tr.getAttribute('data-href'),
                        class: tr.getAttribute('class'),
                    },
                }));
            }""")
            results["mn_rows"] = {"url": page.url, "rows": mn_info}
        except Exception as exc:
            results["mn_rows"] = {"url": mn_url, "error": str(exc)[:300]}

    return JSONResponse(results)


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


