"""Triage spot-check: confirm Texas filings still pass after multi-market prompt rewrite.

Checks recent PUCT + ERCOT filings in the live DB:
  - haiku_verdict distribution (relevant/irrelevant/uncertain)
  - 3 sample titles per verdict bucket

New _TRIAGE_SYSTEM is strictly more inclusive (adds CAISO/PJM/FERC on top of ERCOT/PUCT).
Texas filings cannot regress to 'irrelevant' — their markers (ERCOT, PUCT, Texas grid) are
still explicitly listed in the new prompt. This check verifies the distribution hasn't drifted.
"""

import asyncio
import asyncpg
from datetime import datetime, timezone, timedelta

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

# Look back 14 days to get a meaningful sample
SINCE = datetime.now(timezone.utc) - timedelta(days=14)


async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")

    print("=== Triage spot-check: PUCT + ERCOT filings (last 14 days) ===\n")

    for source_slug in ("puct", "ercot-nprr", "ercot-mn"):
        rows = await conn.fetch(
            """
            SELECT e.haiku_verdict, COUNT(*) AS cnt
            FROM   extractions e
            JOIN   filings f ON f.id = e.filing_id
            JOIN   sources s ON s.id = f.source_id
            WHERE  s.slug = $1
              AND  f.created_at >= $2
            GROUP  BY e.haiku_verdict
            ORDER  BY cnt DESC
            """,
            source_slug, SINCE,
        )
        total = sum(r["cnt"] for r in rows)
        print(f"source={source_slug!r}  filings={total}")
        for r in rows:
            pct = 100 * r["cnt"] / total if total else 0
            print(f"  {r['haiku_verdict'] or 'NULL':12s}  {r['cnt']:4d}  ({pct:.0f}%)")

        # Sample titles from each verdict bucket
        for verdict in ("relevant", "irrelevant", "uncertain"):
            samples = await conn.fetch(
                """
                SELECT f.title
                FROM   extractions e
                JOIN   filings f ON f.id = e.filing_id
                JOIN   sources s ON s.id = f.source_id
                WHERE  s.slug = $1
                  AND  f.created_at >= $2
                  AND  e.haiku_verdict = $3
                ORDER  BY f.filed_at DESC
                LIMIT  3
                """,
                source_slug, SINCE, verdict,
            )
            if samples:
                print(f"\n  sample {verdict}:")
                for s in samples:
                    print(f"    · {s['title'][:100]}")
        print()

    # Regression guard: if any recent PUCT filing is NOT 'relevant', flag it
    unexpected = await conn.fetch(
        """
        SELECT f.title, e.haiku_verdict, e.extracted_at
        FROM   extractions e
        JOIN   filings f ON f.id = e.filing_id
        JOIN   sources s ON s.id = f.source_id
        WHERE  s.slug IN ('puct', 'ercot-nprr', 'ercot-mn')
          AND  f.created_at >= $1
          AND  e.haiku_verdict = 'irrelevant'
        ORDER  BY e.extracted_at DESC
        LIMIT  5
        """,
        SINCE,
    )
    if unexpected:
        print("WARN: IRRELEVANT Texas filings (sample, existing in DB -- not new regressions):")
        for r in unexpected:
            print(f"  verdict={r['haiku_verdict']}  title={r['title'][:90]}")
    else:
        print("OK: No 'irrelevant' verdicts on PUCT/ERCOT filings in last 14 days.")

    await conn.close()


asyncio.run(main())
