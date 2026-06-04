"""Read diagnose-ferc job results — ER24-2236 RTEP filing list."""
import asyncio, asyncpg, json

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")
    rows = await conn.fetch("""
        SELECT j.id::text, j.status, jr.output::text, jr.finished_at
        FROM jobs j LEFT JOIN job_results jr ON jr.job_id = j.id
        WHERE j.kind = 'diagnose-ferc'
        ORDER BY j.created_at DESC LIMIT 2
    """)
    for r in rows:
        print(f"\n=== job {r['id'][:8]}  status={r['status']}  finished={str(r['finished_at'])[:19] if r['finished_at'] else 'pending'} ===")
        if not r["output"]:
            print("  (no output yet)")
            continue
        out = json.loads(r["output"])
        for docket, data in out.items():
            if not isinstance(data, dict):
                continue
            if "error" in data:
                print(f"\n  [{docket}] ERROR: {data['error']}")
                continue
            filings = data.get("filings", [])
            print(f"\n  [{docket}] totalHits={data.get('totalHits')}  filings={len(filings)}")
            for f in filings:
                print(f"    {f['acc']}  {f['filed']}  {f['filer']}")
                print(f"      doc_type={f['doc_type_raw']}  file_id={f['file_id']}")
                print(f"      desc: {f['desc'][:80]}")
    await conn.close()

asyncio.run(main())
