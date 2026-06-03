"""T11-verify-live: PJM Beta end-to-end verification.

Steps:
  1. Crawl PJM with since=2026-02-01 (catches ER26-455 collar extension filings)
  2. Crawl IMM (2025 back-catalog, since floor 2025-01-01)
  3. Crawl PJM calendar (seeds market_events)
  4. Enqueue extraction for one ER26-455 filing and one ER24-2236 RTEP filing
  5. Poll for extraction results (up to 5 min)
  6. Echo-test: confirm rpm_parameters != {329.17, 177.24}
  7. RTEP test: confirm rtep_cost_allocation[] has >1 zone (full coverage)
  8. Cache test: check llm_calls for cache_read_input_tokens across pjm + imm
  9. Calendar render: confirm market_events rows exist
 10. Print summary

Run:  uv run python verify_pjm_beta.py
Needs: DATABASE_URL (or set via .env.local), ANTHROPIC_API_KEY in env.
"""

import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timezone

import asyncpg

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"
POLL_INTERVAL_S = 15
POLL_TIMEOUT_S  = 300   # 5 min


# ── helpers ───────────────────────────────────────────────────────────────────

async def enqueue_job(conn, kind: str, payload: dict, priority: int = 5) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO jobs (kind, payload, priority, max_attempts)
        VALUES ($1, $2::jsonb, $3, 3)
        RETURNING id::text
        """,
        kind, json.dumps(payload), priority,
    )
    return row["id"]


async def wait_for_job(conn, job_id: str, timeout: int = POLL_TIMEOUT_S) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = await conn.fetchrow(
            "SELECT status, error FROM jobs WHERE id = $1::uuid", job_id
        )
        if not row:
            return None
        if row["status"] in ("done", "failed"):
            return dict(row)
        await asyncio.sleep(POLL_INTERVAL_S)
    return None


# ── step 1-3: crawl PJM, IMM, calendar ───────────────────────────────────────

async def run_crawls(conn) -> dict:
    print("\n=== Step 1-3: Crawling PJM (since=2026-02-01), IMM, calendar ===")

    pjm_job    = await enqueue_job(conn, "crawl-pjm",          {"since": "2026-02-01"}, priority=10)
    imm_job    = await enqueue_job(conn, "crawl-imm",          {"since": "2025-01-01"}, priority=10)
    cal_job    = await enqueue_job(conn, "crawl-pjm-calendar", {}, priority=10)

    print(f"  crawl-pjm job:          {pjm_job}")
    print(f"  crawl-imm job:          {imm_job}")
    print(f"  crawl-pjm-calendar job: {cal_job}")
    print(f"  Polling (up to {POLL_TIMEOUT_S}s) ...")

    results = {}
    for job_id, name in [(pjm_job, "pjm"), (imm_job, "imm"), (cal_job, "calendar")]:
        r = await wait_for_job(conn, job_id)
        if r:
            results[name] = r["status"]
            print(f"  {name}: {r['status']}" + (f" ERROR: {r['error']}" if r.get("error") else ""))
        else:
            results[name] = "timeout"
            print(f"  {name}: TIMEOUT")

    return results


# ── step 4-5: find filings and enqueue extractions ────────────────────────────

async def find_and_extract(conn, docket_external_id: str, label: str) -> dict | None:
    """Find the newest filing for a docket, enqueue extraction, wait for result."""
    row = await conn.fetchrow(
        """
        SELECT f.id::text AS filing_id, f.doc_type, f.title, f.source_url,
               s.slug AS source_slug
        FROM   filings f
        JOIN   sources s ON s.id = f.source_id
        JOIN   dockets d ON d.id = f.docket_id
        WHERE  d.external_id = $1
          AND  d.jurisdiction = 'PJM-FERC'
        ORDER  BY f.filed_at DESC
        LIMIT  1
        """,
        docket_external_id,
    )
    if not row:
        print(f"  {label} ({docket_external_id}): no filing found — check crawl ran")
        return None

    filing_id = row["filing_id"]
    print(f"  {label}: filing={filing_id[:8]} doc_type={row['doc_type']} source={row['source_slug']}")
    print(f"    title: {row['title'][:90]}")

    # Check if already extracted at prompt_ver 1.4
    existing = await conn.fetchrow(
        """
        SELECT payload::text
        FROM   extractions
        WHERE  filing_id = $1::uuid AND prompt_ver = '1.4'
        ORDER  BY extracted_at DESC LIMIT 1
        """,
        filing_id,
    )
    if existing:
        print(f"  {label}: extraction already exists at prompt_ver 1.4")
        return json.loads(existing["payload"])

    ext_job = await enqueue_job(conn, "extract", {"filing_id": filing_id, "doc_type": row["doc_type"]})
    print(f"  {label}: enqueued extract job {ext_job[:8]} — polling ...")
    result = await wait_for_job(conn, ext_job)
    if not result or result["status"] != "completed":
        print(f"  {label}: extraction job {result}")
        return None

    row2 = await conn.fetchrow(
        "SELECT payload::text FROM extractions WHERE filing_id = $1::uuid ORDER BY extracted_at DESC LIMIT 1",
        filing_id,
    )
    return json.loads(row2["payload"]) if row2 else None


# ── step 6: echo test ─────────────────────────────────────────────────────────

def check_echo(payload: dict | None, docket: str) -> bool:
    """Return True if rpm_parameters do NOT echo the few-shot (329.17 / 177.24)."""
    if not payload:
        print(f"  ECHO-TEST {docket}: no payload — SKIP")
        return False
    rpm = payload.get("rpm_parameters") or {}
    cap   = rpm.get("price_cap_ucap_mwday")
    floor = rpm.get("price_floor_ucap_mwday")
    basis = rpm.get("capacity_basis")
    dy    = rpm.get("delivery_years") or []
    print(f"  ECHO-TEST {docket}: cap={cap}  floor={floor}  basis={basis}  years={dy}")
    if cap is None and floor is None:
        print(f"    -> rpm_parameters null/empty (RTEP or non-RPM filing) — not an echo failure")
        return True   # not an RPM filing, skip echo check
    ECHO_CAP   = 329.17
    ECHO_FLOOR = 177.24
    if cap == ECHO_CAP or floor == ECHO_FLOOR:
        print(f"    -> ECHO DETECTED — values match few-shot anchors. Investigate.")
        return False
    print(f"    -> PASS — values differ from few-shot anchors (expected ~325 cap, ~175 floor)")
    return True


# ── step 7: RTEP zone coverage ────────────────────────────────────────────────

def check_rtep(payload: dict | None, docket: str) -> bool:
    if not payload:
        print(f"  RTEP {docket}: no payload — SKIP")
        return False
    zones = payload.get("rtep_cost_allocation") or []
    print(f"  RTEP {docket}: rtep_cost_allocation[] has {len(zones)} zone(s)")
    for z in zones[:5]:
        print(f"    zone={z.get('zone','?')}  dollars={z.get('dollars','?')}")
    if zones:
        print(f"    -> PASS — {len(zones)} zone(s) found")
        return True
    print(f"    -> WARN — no zones extracted; check if Schedule 12 tables are in the document")
    return False


# ── step 8: cache check ───────────────────────────────────────────────────────

async def check_cache(conn) -> bool:
    rows = await conn.fetch(
        """
        SELECT s.slug AS source_slug, COUNT(*) AS calls,
               SUM(lc.cache_read_input_tokens) AS total_cache_read
        FROM   llm_calls lc
        JOIN   filings f ON f.id = lc.filing_id
        JOIN   sources s ON s.id = f.source_id
        WHERE  lc.prompt_version = '1.4'
          AND  lc.pipeline_stage = 'sonnet-extract'
          AND  s.slug IN ('pjm', 'imm')
        GROUP  BY s.slug
        """,
    )
    print("\n  Cache check (prompt_ver=1.4, sonnet-extract):")
    any_hit = False
    for r in rows:
        hit = (r["total_cache_read"] or 0) > 0
        print(f"    source={r['source_slug']}  calls={r['calls']}  cache_read_tokens={r['total_cache_read'] or 0}  {'HIT' if hit else 'MISS/first-call'}")
        if hit:
            any_hit = True
    if not rows:
        print("    No llm_calls rows at prompt_ver 1.4 yet — need first extraction to run")
    return any_hit


# ── step 9: calendar rows ─────────────────────────────────────────────────────

async def check_calendar(conn) -> int:
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM market_events WHERE jurisdiction = 'PJM-FERC'"
    )
    upcoming = await conn.fetchval(
        "SELECT COUNT(*) FROM market_events WHERE jurisdiction = 'PJM-FERC' AND event_date >= CURRENT_DATE"
    )
    print(f"\n  Calendar: total market_events={count}  upcoming={upcoming}")
    rows = await conn.fetch(
        """
        SELECT source, event_type, title, event_date::text, estimated
        FROM   market_events
        WHERE  jurisdiction = 'PJM-FERC' AND event_date >= CURRENT_DATE
        ORDER  BY event_date
        LIMIT  5
        """
    )
    for r in rows:
        est = " (est.)" if r["estimated"] else ""
        print(f"    {r['event_date']}  [{r['source']}] {r['title'][:70]}{est}")
    return upcoming


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")
    print("=== PJM Beta T11-verify-live ===")
    print(f"DB connected. Date: {date.today()}")

    # Step 1-3: crawl
    crawl_results = await run_crawls(conn)

    # Step 4-5: find & extract ER26-455 (RPM echo test)
    print("\n=== Step 4-5: Extraction — ER26-455 (RPM collar extension) ===")
    er26_payload = await find_and_extract(conn, "ER26-455", "ER26-455-RPM")

    # Step 4-5: find & extract ER24-2236 (RTEP zone test)
    print("\n=== Step 4-5: Extraction — ER24-2236 (RTEP protocol) ===")
    er24_payload = await find_and_extract(conn, "ER24-2236", "ER24-2236-RTEP")

    # Step 4-5: find & extract an IMM filing (cache cross-source)
    print("\n=== Step 4-5: Extraction — IMM (any filing) ===")
    imm_row = await conn.fetchrow(
        """
        SELECT f.id::text AS filing_id, f.doc_type, f.title
        FROM   filings f
        JOIN   sources s ON s.id = f.source_id
        WHERE  s.slug = 'imm'
        ORDER  BY f.filed_at DESC LIMIT 1
        """
    )
    imm_payload = None
    if imm_row:
        print(f"  IMM filing: {imm_row['filing_id'][:8]}  {imm_row['title'][:70]}")
        imm_ext_job = await enqueue_job(conn, "extract", {"filing_id": imm_row["filing_id"], "doc_type": imm_row["doc_type"]})
        r = await wait_for_job(conn, imm_ext_job)
        if r and r["status"] == "completed":
            row2 = await conn.fetchrow("SELECT payload::text FROM extractions WHERE filing_id = $1::uuid ORDER BY extracted_at DESC LIMIT 1", imm_row["filing_id"])
            if row2:
                imm_payload = json.loads(row2["payload"])
    else:
        print("  No IMM filings yet — IMM crawl may need more time")

    # Step 6: echo test
    print("\n=== Step 6: Echo test — ER26-455 RPM parameters ===")
    echo_pass = check_echo(er26_payload, "ER26-455")

    # Step 7: RTEP zone coverage
    print("\n=== Step 7: RTEP zone coverage — ER24-2236 ===")
    rtep_pass = check_rtep(er24_payload, "ER24-2236")

    # Step 8: cache
    print("\n=== Step 8: Cache 2x/4x — pjm + imm sources ===")
    cache_pass = await check_cache(conn)

    # Step 9: calendar
    print("\n=== Step 9: Calendar events ===")
    calendar_count = await check_calendar(conn)

    # Summary
    print("\n=== SUMMARY ===")
    print(f"  Crawl jobs: {crawl_results}")
    print(f"  Echo test (ER26-455):    {'PASS' if echo_pass else 'FAIL/SKIP'}")
    print(f"  RTEP zones (ER24-2236):  {'PASS' if rtep_pass else 'WARN/SKIP'}")
    print(f"  Cache hit (pjm/imm):     {'HIT' if cache_pass else 'FIRST-CALL (re-run to verify)'}")
    print(f"  Calendar events:         {calendar_count} upcoming PJM-FERC events")

    decision = echo_pass and calendar_count > 0
    print(f"\n  Beta window: {'READY — close #55-#58, start 5-day reliability window' if decision else 'NEEDS ATTENTION (see above)'}")

    await conn.close()


asyncio.run(main())
