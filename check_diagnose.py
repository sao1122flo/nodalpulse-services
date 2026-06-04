"""Read diagnose-ferc job results — DownloadP8File probe."""
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
            if probe == "session_get":
                print(f"    status={data.get('status')}  cookies={data.get('cookies')}  ct={data.get('ct')}")
                continue
            if probe == "filelist":
                print(f"    status={data.get('status')}  ct={data.get('ct')}  len={data.get('len')}")
                print(f"    preview: {data.get('preview','')[:300]}")
                continue
            # PDF/P8 probes
            print(f"    acc={data.get('acc')}  status={data.get('status')}  len={data.get('len')}  is_pdf={data.get('is_pdf')}")
            print(f"    ct={data.get('ct')}")
            if data.get("is_pdf"):
                print(f"    *** PDF CONFIRMED ***")
            else:
                t = data.get("preview_text", "")[:200]
                if t:
                    print(f"    text: {t}")
    await conn.close()

asyncio.run(main())
