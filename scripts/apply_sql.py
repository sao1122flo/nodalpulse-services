import asyncio, asyncpg, os, sys

async def main():
    dsn = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(dsn)
    with open(sys.argv[1]) as f:
        await conn.execute(f.read())
    await conn.close()
    print(f'Applied: {sys.argv[1]}')

asyncio.run(main())