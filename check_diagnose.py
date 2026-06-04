"""Read diagnose-ferc job results from the DB."""
import asyncio, asyncpg, json

DB_URL = "postgresql://postgres:IYMFvbJloSVssntQgRkIdoBMXXjPihtS@trolley.proxy.rlwy.net:35031/railway"

async def main():
    conn = await asyncpg.connect(DB_URL, ssl="require")
    rows = await conn.fetch("""
        SELECT j.id::text, j.status, jr.output::text, jr.finished_at
        FROM   jobs j LEFT JOIN job_results jr ON jr.job_id = j.id
        WHERE  j.kind = 'diagnose-ferc'
        ORDER  BY j.created_at DESC LIMIT 3
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
            print(f"    status={data.get('status','?')}  totalHits={data.get('totalHits','?')}")
            if "first_item_keys" in data:
                print(f"    field_names: {data['first_item_keys']}")
            if "first_item" in data and data["first_item"]:
                item = data["first_item"]
                for k, v in item.items():
                    sv = str(v)[:120]
                    print(f"      {k}: {sv}")
            if "first_3_descriptions" in data:
                for d in data["first_3_descriptions"]:
                    print(f"      desc: {str(d)[:100]}")
            if "first_3_dockets" in data:
                print(f"      dockets: {data['first_3_dockets']}")
            if "is_pdf" in data:
                print(f"    accession={data.get('accession_used')}  is_pdf={data['is_pdf']}  len={data.get('len')}  ct={data.get('content_type')}")
            if "skipped" in data:
                print(f"    SKIPPED: {data['skipped']}  probe1_keys={data.get('probe1_keys')}")
            if "preview" in data:
                print(f"    preview: {data['preview'][:300]}")
    await conn.close()

asyncio.run(main())
