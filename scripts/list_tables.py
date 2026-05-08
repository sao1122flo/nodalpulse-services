import asyncio
import os
import asyncpg

async def main():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    print("Tables:", [r["tablename"] for r in rows])
    await conn.close()

asyncio.run(main())
