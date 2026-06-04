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
            if data.get("skipped"):
                print(f"    SKIPPED  p1_keys={data.get('p1_keys')}")
                continue
            # PDF probes
            if "is_pdf" in data:
                print(f"    status={data.get('status')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('ct')}")
                if data.get("preview"):
                    print(f"    preview: {data['preview'][:200]}")
                continue
            # Affiliation probe
            if "totalHits" in data:
                print(f"    status={data.get('status')}  totalHits={data.get('totalHits')}  items_count={data.get('items_count')}")
                for i, (affils, dockets, desc) in enumerate(zip(
                    data.get("first_3_affils", []),
                    data.get("first_3_dockets", []),
                    data.get("first_3_descriptions", []),
                )):
                    print(f"    [{i}] affils={affils}  dockets={dockets}")
                    print(f"         desc: {desc[:100]}")
                continue
            # Generic
            print(f"    {json.dumps(data)[:400]}")
    await conn.close()

asyncio.run(main())
