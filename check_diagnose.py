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
            err = data.get("error")
            if err:
                print(f"    ERROR: {err}")
                continue
            if data.get("parse_error"):
                print(f"    PARSE_ERROR: {data['parse_error']}")
                print(f"    preview: {data.get('preview','')[:200]}")
                continue
            if data.get("skipped"):
                print(f"    SKIPPED: {data['skipped']}")
                print(f"    probe1_item_keys: {data.get('probe1_item_keys')}")
                continue
            print(f"    status={data.get('status','?')}  totalHits={data.get('total_hits_raw','?')}  items_key={data.get('items_key_found')}  items_count={data.get('items_count')}")
            print(f"    top_keys: {data.get('top_keys')}")
            print(f"    first_item_keys: {data.get('first_item_keys')}")
            item = data.get("first_item", {})
            if item:
                for k, v in item.items():
                    print(f"      {k}: {str(v)[:120]}")
            small = data.get("full_json_if_small")
            if small and small != "<too large>" and not item:
                print(f"    full_json: {json.dumps(small)[:800]}")
            # PDF probe
            if "is_pdf" in data:
                print(f"    accession={data.get('accession_used')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('content_type')}")
    await conn.close()

asyncio.run(main())
