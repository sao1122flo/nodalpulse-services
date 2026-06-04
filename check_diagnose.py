"""Read diagnose-ferc job results from the DB."""
import asyncio, asyncpg, json

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")
    rows = await conn.fetch("""
        SELECT j.id::text, j.status, jr.output::text, jr.finished_at
        FROM   jobs j LEFT JOIN job_results jr ON jr.job_id = j.id
        WHERE  j.kind = 'diagnose-ferc'
        ORDER  BY j.created_at DESC LIMIT 2
    """)
    for r in rows:
        print(f"\n=== job {r['id'][:8]}  status={r['status']}  finished={str(r['finished_at'])[:19] if r['finished_at'] else 'pending'} ===")
        if not r["output"]:
            print("  (no output yet)")
            continue
        out = json.loads(r["output"])
        for probe, data in out.items():
            if not isinstance(data, dict):
                continue
            print(f"\n  [{probe}]")
            if data.get("error"):
                print(f"    ERROR: {data['error']}")
                continue
            # PDF probe
            if "is_pdf" in data:
                print(f"    status={data.get('status')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('ct')}")
                if data.get("preview"):
                    print(f"    preview: {data['preview'][:150]}")
                continue
            # Search probe
            if "totalHits" in data:
                print(f"    status={data.get('status')}  totalHits={data.get('totalHits')}  items_count={data.get('items_count')}")
                affils = data.get("first_3_affils", [])
                dockets = data.get("first_3_dockets", [])
                descs = data.get("first_3_desc", [])
                for i in range(len(descs)):
                    print(f"    [{i}] affils={affils[i] if i < len(affils) else '?'}")
                    print(f"         dockets={dockets[i] if i < len(dockets) else '?'}")
                    print(f"         desc: {descs[i][:100]}")
                continue
            print(f"    {json.dumps(data)[:300]}")
    await conn.close()

asyncio.run(main())
