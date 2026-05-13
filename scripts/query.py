import asyncio, asyncpg, os, sys

async def main():
    dsn = (os.environ.get('DATABASE_PUBLIC_URL') or os.environ['DATABASE_URL']).replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(sys.argv[1])
    for r in rows:
        print(dict(r))
    await conn.close()

asyncio.run(main())
