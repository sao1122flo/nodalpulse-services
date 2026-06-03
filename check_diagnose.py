"""Read diagnose-ferc job results from the DB."""
import asyncio, asyncpg, json

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")
    rows = await conn.fetch("""
        SELECT j.status, jr.output::text, jr.finished_at
        FROM   jobs j LEFT JOIN job_results jr ON jr.job_id = j.id
        WHERE  j.kind = 'diagnose-ferc'
        ORDER  BY j.created_at DESC LIMIT 8
    """)
    for r in rows:
        out = json.loads(r["output"]) if r["output"] else {}
        print(f"  {r['status']:8s}  {str(r['finished_at'])[:19] if r['finished_at'] else 'pending':19s}"
              f"  {out.get('year','?')}-{str(out.get('month','?')).zfill(2)}"
              f"  http={out.get('http_status','?')}  len={out.get('body_len','?')}"
              f"  items={out.get('item_count','?')}")
        for t in out.get("first_5_titles", []):
            print(f"    title: {t}")
        if out.get("body_preview"):
            print(f"    body[:300]: {out['body_preview'][:300]}")
        if out.get("error"):
            print(f"    ERROR: {out['error']}")
        # New multi-URL format
        for name, r in out.items() if isinstance(out, dict) else []:
            if isinstance(r, dict) and "url" in r:
                err = r.get("error", "")
                print(f"  {name:20s}  status={r.get('status','?'):3}  len={r.get('len','?'):7}  rss={r.get('is_rss','?')}  er/el={r.get('has_er_el','?')}  {err or ''}")
                if r.get("preview"):
                    print(f"    preview: {r['preview'][:150]}")
    await conn.close()

asyncio.run(main())
