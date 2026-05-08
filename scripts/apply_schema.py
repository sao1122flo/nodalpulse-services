"""Apply schema.sql to the Railway Postgres database."""

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

    # asyncpg uses postgresql:// not postgresql+asyncpg://
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    schema_path = Path(__file__).parent.parent / "nodalpulse-shared" / "sql" / "schema.sql"
    if not schema_path.exists():
        # Try relative to cwd
        schema_path = Path("../nodalpulse-shared/sql/schema.sql")
    if not schema_path.exists():
        print(f"schema.sql not found at {schema_path}", file=sys.stderr)
        sys.exit(1)

    sql = schema_path.read_text(encoding="utf-8")
    print(f"Connecting to {db_url[:40]}...")
    conn = await asyncpg.connect(db_url)
    try:
        print("Applying schema.sql ...")
        await conn.execute(sql)
        print("Schema applied successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
