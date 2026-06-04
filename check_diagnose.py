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
            if probe == "full_transmittals":
                print(f"    status={data.get('status')}  acc={data.get('acesssionNumber')}  n_transmittals={data.get('transmittals_count')}")
                for t in data.get("transmittals", []):
                    print(f"    transmittal: {json.dumps(t)}")
                continue
            if "is_pdf" in data:
                print(f"    status={data.get('status')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('ct')}")
                if data.get("preview"):
                    print(f"    preview: {data['preview'][:150]}")
                continue
            if "totalHits" in data:
                print(f"    status={data.get('status')}  totalHits={data.get('totalHits')}  numHits={data.get('numHits')}  items={data.get('items_count')}")
                for item in data.get("first_3", []):
                    print(f"    acc={item.get('acc')}  filed={item.get('filed')}")
                    print(f"      dockets={item.get('dockets')}  affils={item.get('affils')}")
                    print(f"      desc: {item.get('desc','')[:100]}")
                continue
            if "fileName" in data:
                print(f"    fileName={data.get('fileName')}  is_url={data.get('is_url')}")
                continue
            print(f"    {json.dumps(data)[:300]}")
    await conn.close()

asyncio.run(main())
