"""DB operations for filing persistence."""

import json
import logging
import re
from datetime import datetime

from sqlalchemy import text

from nodalpulse.crawlers.base import RawFiling
from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)

# FERC dockets use a base-docket + optional subdocket suffix, e.g. ER26-2267-000.
# The FERC API always returns filings tagged with the BASE docket (ER26-2267), even
# when the AdvancedSearch query specifies a subdocket.  Storing both creates a split:
# user tracks ER26-2267-000 (UUID A) but filings link to ER26-2267 (UUID B) → briefs
# return 0 results for UUID A.  Canonicalize to the base before any DB write.
_FERC_SUBDOCKET_RE = re.compile(r"^([A-Z]{2}\d{2}-\d+)-\d{3}$")
_FERC_JURISDICTIONS = frozenset({"FERC", "PJM-FERC", "CAISO-FERC"})


def _canonicalize_ferc_docket(docket_number: str, jurisdiction: str | None) -> str:
    """Strip 3-digit subdocket suffix for FERC-family dockets.

    ER26-2267-000 → ER26-2267
    EL24-119      → EL24-119  (2 segments — unchanged)
    ER26-2267-001 → ER26-2267 (all subdockets collapse to base proceeding)
    Non-FERC jurisdictions are returned unchanged.
    """
    if jurisdiction not in _FERC_JURISDICTIONS:
        return docket_number
    m = _FERC_SUBDOCKET_RE.match(docket_number)
    return m.group(1) if m else docket_number


async def get_source_id(slug: str) -> str | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id::text FROM sources WHERE slug = :slug"),
            {"slug": slug},
        )
        return result.scalar_one_or_none()


async def get_existing_item_keys(source_id: str, item_keys: list[str]) -> set[str]:
    """Return subset of item_keys already present in filings (metadata->>'item_key')."""
    if not item_keys:
        return set()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT metadata->>'item_key'
                FROM filings
                WHERE source_id = CAST(:source_id AS uuid)
                  AND metadata->>'item_key' = ANY(:item_keys)
            """),
            {"source_id": source_id, "item_keys": item_keys},
        )
        return {row[0] for row in result.fetchall() if row[0]}


async def get_last_crawled_at(source_slug: str) -> str | None:
    """Return ISO date string of the newest filing we have for this source."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT MAX(filed_at)::date::text FROM filings
                WHERE source_id = (SELECT id FROM sources WHERE slug = :slug)
            """),
            {"slug": source_slug},
        )
        return result.scalar_one_or_none()


async def stamp_last_crawled_at(
    docket_ids: set[str] | None = None,
    *,
    source_id: str | None = None,
    external_ids: set[str] | None = None,
) -> None:
    """Mark dockets as crawled NOW — the durable per-docket signal that separates
    "crawled, 0 results" from "never crawled" for the Record page's warming states.

    Two complementary scopes, both stamped even on a 0-result crawl:
    - docket_ids: dockets that received >=1 filing this run (touched) — universal,
      set by every crawl (scheduled + on-demand) for populated dockets.
    - external_ids (with source_id): dockets the crawl explicitly *targeted* (on-demand
      control_numbers, or a watch set) whether or not they produced filings — this is
      what stamps a genuinely-empty tracked docket so it shows "No filings yet", not a
      perpetual spinner.
    """
    async with AsyncSessionLocal() as session:
        if docket_ids:
            await session.execute(
                # id::text (not id) so the text[] param matches without a uuid cast.
                text("UPDATE dockets SET last_crawled_at = NOW() WHERE id::text = ANY(:ids)"),
                {"ids": list(docket_ids)},
            )
        if external_ids and source_id:
            await session.execute(
                text(
                    "UPDATE dockets SET last_crawled_at = NOW() "
                    "WHERE source_id = :sid AND external_id = ANY(:exts)"
                ),
                {"sid": source_id, "exts": list(external_ids)},
            )
        await session.commit()


async def find_or_create_docket(
    source_id: str,
    docket_number: str,
    jurisdiction: str | None = None,
    title: str | None = None,
) -> str:
    """Find or create a dockets row for this source + docket_number.

    Returns the docket UUID. Safe under concurrent writes: ON CONFLICT DO UPDATE
    always fires the RETURNING clause.

    jurisdiction: stamped on INSERT; backfills NULL rows on conflict (safe no-op
    for rows already carrying a jurisdiction value).
    title: populated on INSERT; backfills NULL rows on conflict (never overwrites
    an existing title — first crawled filing wins, consistent with PUCT convention).

    FERC-family dockets are canonicalized before write: ER26-2267-000 → ER26-2267.
    This prevents the split where user_dockets points to the subdocket UUID while
    filing_dockets points to the base docket UUID (see _canonicalize_ferc_docket).
    """
    docket_number = _canonicalize_ferc_docket(docket_number, jurisdiction)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO dockets (source_id, external_id, status, jurisdiction, title)
                VALUES (CAST(:source_id AS uuid), :external_id, 'open', :jurisdiction, :title)
                ON CONFLICT (source_id, external_id) DO UPDATE
                  SET updated_at   = NOW(),
                      jurisdiction = COALESCE(dockets.jurisdiction, EXCLUDED.jurisdiction),
                      title        = COALESCE(dockets.title, EXCLUDED.title)
                RETURNING id::text
            """),
            {
                "source_id": source_id,
                "external_id": docket_number,
                "jurisdiction": jurisdiction,
                "title": title,
            },
        )
        docket_id = result.scalar_one()
        await session.commit()
        return docket_id


async def get_ferc_docket_set() -> set[str]:
    """Return docket external_ids for the shared FERC + CAISO-FERC watch set.

    Scoped to 'FERC' and 'CAISO-FERC' only. PJM-FERC is intentionally excluded:
    PJM uses handle_crawl_pjm / get_pjm_ferc_docket_set() so that PJM filings are
    stored exclusively under the 'pjm' source. Including PJM-FERC here would cause
    the same FERC accession number to be inserted under both 'ferc' and 'pjm' source
    IDs — a genuine duplicate since both sources pull from the same RSS feed.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT external_id
                FROM dockets
                WHERE jurisdiction IN ('FERC', 'CAISO-FERC')
            """),
        )
        return {row[0] for row in result.fetchall()}


async def get_cpuc_docket_set() -> set[str]:
    """Return normalized CPUC proceeding numbers for the daily crawl watch set.

    Queries all dockets with jurisdiction='CPUC' regardless of which source created them
    (CAISO cross-ref extraction stores them under the CAISO source; manually seeded dockets
    use the cpuc source). Normalizes dots/dashes to match the search-form format:
    A.25-08-008 → A2508008.
    """
    import re

    _norm_re = re.compile(r"[.\-\s]")
    _valid_re = re.compile(r"^[A-Z][0-9]{5,}$")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT external_id FROM dockets WHERE jurisdiction = 'CPUC'"),
        )
        normalized: set[str] = set()
        for (raw,) in result.fetchall():
            n = _norm_re.sub("", (raw or "").strip().upper())
            if _valid_re.match(n):
                normalized.add(n)
        return normalized


async def get_mdpsc_docket_set() -> set[str]:
    """Return MD PSC case numbers (jurisdiction='MD-PSC') — the electric-case watch set.

    These are cases an electric utility has filed in, registered as dockets on prior
    crawls. The MD adapter unions this with the electric cases it discovers in the
    current window, so Commission orders / intervener filings land for known electric
    cases even when no utility files in the same window.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT external_id FROM dockets WHERE jurisdiction = 'MD-PSC'"),
        )
        return {(raw or "").strip() for (raw,) in result.fetchall() if (raw or "").strip()}


async def get_vascc_docket_set() -> set[str]:
    """Return VA SCC case numbers (jurisdiction='VA-SCC') — the electric-case watch set.

    The VA adapter scopes electric coverage primarily by CaseName (every row carries
    the case's regulated utility), but unions this watch set so a hand-added case whose
    CaseName is not an electric utility (e.g. a developer-named transmission / data-
    center docket) is still captured in full across windows.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT external_id FROM dockets WHERE jurisdiction = 'VA-SCC'"),
        )
        return {(raw or "").strip() for (raw,) in result.fetchall() if (raw or "").strip()}


async def get_pjm_ferc_docket_set() -> set[str]:
    """Return curated PJM-FERC docket external_ids for the daily crawl watch set.

    Filters watched=true to return only intentionally-seeded dockets, NOT the
    371-item explosion from multi-docket caption cross-refs. The watched flag
    is set by sql/add_docket_watched_flag.sql for explicitly seeded dockets;
    find_or_create_docket() defaults new rows to watched=false.

    Falls back to all PJM-FERC dockets if the watched column doesn't exist yet
    (before the migration is applied).
    """
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(
                text(
                    "SELECT external_id FROM dockets WHERE jurisdiction = 'PJM-FERC' AND watched = true"
                ),
            )
            rows = result.fetchall()
            if rows:
                return {row[0] for row in rows}
        except Exception:
            pass
        # Fallback: watched column not yet migrated
        result = await session.execute(
            text("SELECT external_id FROM dockets WHERE jurisdiction = 'PJM-FERC'"),
        )
        return {row[0] for row in result.fetchall()}


async def get_all_tracked_docket_ids() -> set[str]:
    """Return internal docket UUIDs tracked by any user across all sources.

    Union of user_dockets junction + legacy user_profiles.tracked_docket_ids array.
    Called once per crawl tick in selective mode to gate Sonnet extraction.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT DISTINCT docket_id::text FROM user_dockets
            UNION
            SELECT DISTINCT unnest(tracked_docket_ids)::text
            FROM user_profiles
            WHERE tracked_docket_ids IS NOT NULL
              AND array_length(tracked_docket_ids, 1) > 0
        """)
        )
        return {row[0] for row in result.fetchall() if row[0]}


async def upsert_filing_dockets(
    filing_id: str,
    docket_ids: list[str],
    first_is_primary: bool = True,
) -> None:
    """Write filing_dockets junction rows for all co-captioned dockets.

    When first_is_primary=True (default): docket_ids[0] is is_primary=True, rest False.
    When first_is_primary=False: all rows are is_primary=False (used for secondary
    cross-refs added at extraction time, e.g. CPUC proceeding refs from CAISO filings).
    Idempotent: ON CONFLICT DO NOTHING (is_primary set on first INSERT).
    """
    if not docket_ids:
        return
    async with AsyncSessionLocal() as session:
        for i, docket_id in enumerate(docket_ids):
            await session.execute(
                text("""
                    INSERT INTO filing_dockets (filing_id, docket_id, is_primary)
                    VALUES (CAST(:filing_id AS uuid), CAST(:docket_id AS uuid), :is_primary)
                    ON CONFLICT (filing_id, docket_id) DO NOTHING
                """),
                {
                    "filing_id": filing_id,
                    "docket_id": docket_id,
                    "is_primary": (i == 0) if first_is_primary else False,
                },
            )
        await session.commit()


async def upsert_filing(
    raw: RawFiling,
    source_id: str,
    r2_key: str | None,  # None when R2 upload is deferred to extraction time (e.g. FERC adapter)
    docket_id: str | None = None,
) -> str | None:
    """Insert filing row. Skips on conflict (same source + external_id). Returns UUID or None.

    If docket_id is provided and the filing already exists with docket_id IS NULL,
    silently backfills docket_id so re-crawled filings get linked without a separate pass.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO filings (
                    source_id, external_id, doc_type, title, filer,
                    filed_at, r2_key, file_ext, source_url, metadata, docket_id
                ) VALUES (
                    CAST(:source_id AS uuid), :external_id, :doc_type, :title, :filer,
                    CAST(:filed_at AS timestamptz), :r2_key, :file_ext, :source_url,
                    CAST(:metadata AS jsonb), CAST(:docket_id AS uuid)
                )
                ON CONFLICT (source_id, external_id) DO UPDATE SET
                    source_url = EXCLUDED.source_url,
                    metadata   = EXCLUDED.metadata
                WHERE EXCLUDED.source_url != ''
                  AND (filings.source_url IS NULL OR filings.source_url = ''
                       OR filings.source_url != EXCLUDED.source_url)
                RETURNING id::text
            """),
            {
                "source_id": source_id,
                "external_id": raw.external_id,
                "doc_type": raw.doc_type,
                "title": raw.title[:500],
                "filer": raw.metadata.get("filer", "")[:500],
                "filed_at": datetime.fromisoformat(raw.filed_at),
                "r2_key": r2_key,
                "file_ext": raw.file_ext,
                "source_url": raw.source_url,
                "metadata": json.dumps(raw.metadata),
                "docket_id": docket_id,
            },
        )
        filing_id = result.scalar_one_or_none()

        if not filing_id and docket_id:
            # Existing filing — backfill docket_id only if still NULL (zero-cost no-op otherwise)
            await session.execute(
                text("""
                    UPDATE filings SET docket_id = CAST(:docket_id AS uuid)
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND external_id = :external_id
                      AND docket_id IS NULL
                """),
                {"docket_id": docket_id, "source_id": source_id, "external_id": raw.external_id},
            )

        await session.commit()
        if filing_id:
            logger.debug("Inserted filing %s → %s", raw.external_id, filing_id)
        return filing_id
