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
            print(f"    status={data.get('status','?')}  top_keys={data.get('top_keys')}  items_key={data.get('items_key')}  items_count={data.get('items_count')}")
            if data.get("first_item_keys"):
                print(f"    first_item_keys: {data['first_item_keys']}")
            item = data.get("first_item", {})
            for k, v in item.items():
                print(f"      {k}: {str(v)[:120]}")
            if data.get("raw_preview"):
                print(f"    raw_preview: {data['raw_preview'][:600]}")
            if "is_pdf" in data:
                print(f"    acc={data.get('acc')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('ct')}")
    await conn.close()

asyncio.run(main())
