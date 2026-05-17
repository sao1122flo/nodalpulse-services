"""Backfill filings.docket_id from filings.metadata->>'control_number'.

Finds or creates a dockets row for each distinct PUCT control_number, then
updates filings.docket_id. Safe to run multiple times (idempotent).

Usage:
    railway run --service Postgres uv run python scripts/backfill_filings_docket_id.py
    railway run --service Postgres uv run python scripts/backfill_filings_docket_id.py --dry-run

Rollback: UPDATE filings SET docket_id = NULL  (recoverable — no rows are deleted)
"""

import argparse
import os
import sys
from urllib.parse import urlparse

import pg8000

PUCT_SOURCE_ID = "0725032a-239f-475d-bdd5-251adad3ae05"
BATCH_SIZE = 500


def run(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_PUBLIC_URL or DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    parsed = urlparse(db_url)
    conn = pg8000.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
        ssl_context=True,
    )
    cur = conn.cursor()

    cur.execute("""
        SELECT id, metadata->>'control_number' AS cn
        FROM filings
        WHERE source_id = %s::uuid
          AND metadata->>'control_number' IS NOT NULL
          AND metadata->>'control_number' != ''
          AND docket_id IS NULL
        ORDER BY filed_at DESC
    """, [PUCT_SOURCE_ID])
    rows = cur.fetchall()

    print(f"Filings to backfill: {len(rows)}")

    if dry_run:
        print("DRY RUN — no writes. Re-run without --dry-run to apply.")
        conn.close()
        return

    docket_cache: dict[str, object] = {}
    updated = 0
    errors = 0

    for filing_id, control_number in rows:
        try:
            if control_number not in docket_cache:
                cur.execute("""
                    INSERT INTO dockets (source_id, external_id, status)
                    VALUES (%s::uuid, %s, 'open')
                    ON CONFLICT (source_id, external_id) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                """, [PUCT_SOURCE_ID, control_number])
                docket_id = cur.fetchone()[0]
                conn.commit()
                docket_cache[control_number] = docket_id
            else:
                docket_id = docket_cache[control_number]

            cur.execute(
                "UPDATE filings SET docket_id = %s WHERE id = %s",
                [docket_id, filing_id],
            )
            updated += 1

            if updated % BATCH_SIZE == 0:
                conn.commit()
                print(f"  {updated}/{len(rows)} updated ({len(docket_cache)} dockets so far)")

        except Exception as exc:
            conn.rollback()
            print(f"  ERROR on filing {filing_id}: {exc}", file=sys.stderr)
            errors += 1

    conn.commit()
    print(
        f"Done: {updated} filings backfilled, "
        f"{len(docket_cache)} dockets created/found, "
        f"{errors} errors"
    )
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Count rows without writing")
    args = parser.parse_args()
    run(args.dry_run)
