"""Read diagnose-ferc job results — sort order + transmittals probe."""
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
        for probe, data in out.items():
            if not isinstance(data, dict):
                continue
            print(f"\n  [{probe}]")
            err = data.get("error")
            if err:
                print(f"    ERROR: {err}")
                continue
            if probe == "sort_order":
                print(f"    totalHits={data.get('totalHits')}")
                print(f"    page1: dates={data.get('page1_dates')}  accs={data.get('page1_accs')}")
                print(f"    page2: dates={data.get('page2_dates')}  accs={data.get('page2_accs')}")
                continue
            if probe.startswith("sort_"):
                print(f"    status={data.get('status')}  dates={data.get('dates')}  accs={data.get('accs')}")
                continue
            if probe.startswith("acc_"):
                print(f"    status={data.get('status')}  acc={data.get('found_acc')}  found={data.get('found', True)}")
                print(f"    filed={data.get('filedDate')}  filer={data.get('filer')}")
                print(f"    desc: {data.get('description','')[:100]}")
                print(f"    dockets: {data.get('docketNumbers')}")
                for t in data.get("transmittals", []):
                    print(f"    transmittal: fileDesc={t.get('fileDesc')}  fileName={t.get('fileName')}  fileId={t.get('fileId')}")
                continue
            print(f"    {json.dumps(data)[:200]}")
    await conn.close()

asyncio.run(main())
