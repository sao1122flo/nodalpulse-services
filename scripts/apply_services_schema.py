"""Apply services_schema.sql to the Railway Postgres database.

Usage:
    DATABASE_URL=postgresql://... python scripts/apply_services_schema.py
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    sql_path = Path(__file__).parent.parent / "sql" / "services_schema.sql"
    if not sql_path.exists():
        print(f"services_schema.sql not found at {sql_path}", file=sys.stderr)
        sys.exit(1)

    sql = sql_path.read_text(encoding="utf-8")
    print(f"Connecting to {db_url[:40]}...")
    conn = await asyncpg.connect(db_url)
    try:
        print("Applying services_schema.sql ...")
        await conn.execute(sql)
        print("Done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
