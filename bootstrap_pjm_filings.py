"""Bootstrap PJM + IMM filings for T11-verify.

Bypasses run_adapter's 3-day lookback cap. Fetches FERC RSS for
Feb-May 2026 directly and persists any ER26-455 / ER26-1556 / ER24-2236
filings found. Also crawls IMM for 2025-2026.

Run locally with VPN active (FERC + IMM are geoblocked in Colombia):
    uv run python bootstrap_pjm_filings.py

Enqueued extract jobs run on the Railway worker automatically.
"""

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone

import asyncpg
import httpx

sys.path.insert(0, "src")

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

# ── import adapters now that src is on path ───────────────────────────────────
from nodalpulse.crawlers.ferc import FercAdapter
from nodalpulse.crawlers.imm import ImmAdapter, _SINCE_FLOOR
from nodalpulse.crawlers.base import RawFiling


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_source_id(conn, slug: str) -> str | None:
    return await conn.fetchval("SELECT id::text FROM sources WHERE slug = $1", slug)


async def get_pjm_docket_set(conn) -> set[str]:
    rows = await conn.fetch("SELECT external_id FROM dockets WHERE jurisdiction = 'PJM-FERC'")
    return {r["external_id"] for r in rows}


async def find_or_create_docket(conn, source_id: str, external_id: str) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO dockets (source_id, external_id, status, jurisdiction)
        VALUES ($1::uuid, $2, 'open', 'PJM-FERC')
        ON CONFLICT (source_id, external_id) DO UPDATE SET updated_at = NOW()
        RETURNING id::text
        """,
        source_id, external_id,
    )
    return row["id"]


async def upsert_filing(conn, raw: RawFiling, source_id: str, docket_id: str | None) -> str | None:
    metadata = json.dumps(raw.metadata)
    filed_at = datetime.fromisoformat(raw.filed_at)
    row = await conn.fetchrow(
        """
        INSERT INTO filings
            (source_id, external_id, doc_type, title, filer, filed_at,
             r2_key, file_ext, source_url, metadata, docket_id)
        VALUES
            ($1::uuid, $2, $3, $4, $5, $6::timestamptz,
             NULL, $7, $8, $9::jsonb, $10::uuid)
        ON CONFLICT (source_id, external_id) DO NOTHING
        RETURNING id::text
        """,
        source_id, raw.external_id, raw.doc_type, raw.title[:500], "",
        filed_at, raw.file_ext, raw.source_url, metadata, docket_id,
    )
    return row["id"] if row else None


async def upsert_filing_dockets(conn, filing_id: str, docket_ids: list[str]) -> None:
    for i, did in enumerate(docket_ids):
        await conn.execute(
            """
            INSERT INTO filing_dockets (filing_id, docket_id, is_primary)
            VALUES ($1::uuid, $2::uuid, $3)
            ON CONFLICT (filing_id, docket_id) DO NOTHING
            """,
            filing_id, did, i == 0,
        )


async def enqueue_extract(conn, filing_id: str, doc_type: str) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO jobs (kind, payload, priority, max_attempts)
        VALUES ('extract', $1::jsonb, 5, 3)
        RETURNING id::text
        """,
        json.dumps({"filing_id": filing_id, "doc_type": doc_type}),
    )
    return row["id"]


# ── persist filings (mirrors run_adapter logic without lookback cap) ──────────

async def persist_filings(conn, filings: list[RawFiling], source_slug: str) -> tuple[int, int]:
    source_id = await get_source_id(conn, source_slug)
    if not source_id:
        print(f"  WARN: source '{source_slug}' not in DB — skipping")
        return 0, 0

    saved = skipped = 0
    for raw in filings:
        docket_refs: list[str] = (
            raw.metadata.get("docket_numbers")
            or ([raw.metadata["control_number"]] if raw.metadata.get("control_number") else [])
            or []
        )
        docket_ids = []
        for ref in docket_refs:
            did = await find_or_create_docket(conn, source_id, ref)
            docket_ids.append(did)

        docket_id = docket_ids[0] if docket_ids else None
        filing_id = await upsert_filing(conn, raw, source_id, docket_id)
        if filing_id:
            if docket_ids:
                await upsert_filing_dockets(conn, filing_id, docket_ids)
            await enqueue_extract(conn, filing_id, raw.doc_type)
            saved += 1
        else:
            skipped += 1

    return saved, skipped


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=== Bootstrap PJM + IMM filings ===")
    conn = await asyncpg.connect(DB_URL, ssl="require")

    # ── PJM FERC (extended lookback: Feb 1 2026) ──────────────────────────────
    print("\n--- PJM FERC: fetching RSS Feb-Jun 2026 ---")
    pjm_set = await get_pjm_docket_set(conn)
    print(f"  PJM-FERC watch set ({len(pjm_set)}): {sorted(pjm_set)}")

    adapter = FercAdapter(pjm_set)
    pjm_filings = await adapter.fetch_new(since="2026-02-01")  # no cap
    print(f"  FERC RSS returned {len(pjm_filings)} filings matching PJM watch set")
    for f in pjm_filings[:10]:
        print(f"    {f.filed_at[:10]}  {f.doc_type:25s}  {f.title[:70]}")

    saved, skipped = await persist_filings(conn, pjm_filings, "pjm")
    print(f"  Saved={saved}  Skipped(dup)={skipped}")

    # ── IMM (full 2025 + 2026 back-catalog) ──────────────────────────────────
    print("\n--- IMM: fetching 2025-2026 ---")
    imm_adapter = ImmAdapter()
    imm_filings = await imm_adapter.fetch_new(since="2025-01-01")  # no cap
    print(f"  IMM returned {len(imm_filings)} filings")
    for f in imm_filings[:8]:
        print(f"    {f.filed_at[:10]}  {f.doc_type:20s}  {f.title[:70]}")

    imm_saved, imm_skipped = await persist_filings(conn, imm_filings, "imm")
    print(f"  Saved={imm_saved}  Skipped(dup)={imm_skipped}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n=== Done ===")
    print(f"  PJM filings:  saved={saved}  skipped={skipped}")
    print(f"  IMM filings:  saved={imm_saved}  skipped={imm_skipped}")
    total = saved + imm_saved
    if total > 0:
        print(f"  {total} extract jobs enqueued — Railway worker will process them.")
        print(f"  Re-run verify_pjm_beta.py in ~5 min to check results.")
    else:
        print("  No new filings found. Check VPN is active and FERC RSS is reachable.")

    await conn.close()


asyncio.run(main())
