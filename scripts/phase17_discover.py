"""Phase 17 discovery queries — run via: railway run --service Postgres uv run python scripts/phase17_discover.py"""
import os, sys, urllib.parse

try:
    import pg8000.native as pg
except ImportError:
    print("pg8000 not available", file=sys.stderr)
    sys.exit(1)

raw = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL", "")
if not raw:
    print("No DATABASE_PUBLIC_URL or DATABASE_URL", file=sys.stderr)
    sys.exit(1)

raw = raw.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
p = urllib.parse.urlparse(raw)
print(f"Connecting to {p.hostname}:{p.port}{p.path}", file=sys.stderr)

conn = pg.Connection(
    host=p.hostname,
    port=p.port or 5432,
    database=p.path.lstrip("/"),
    user=p.username,
    password=p.password,
    ssl_context=True,
)

print("\n=== jobs by kind/status ===")
rows = conn.run("SELECT kind, status, COUNT(*) as cnt FROM jobs GROUP BY kind, status ORDER BY kind, status")
print("kind | status | count")
for r in rows:
    print(r)

print("\n=== llm_calls indexes ===")
rows2 = conn.run("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'llm_calls'")
for r in rows2:
    print(r)

print("\n=== last-hour llm spend (test query) ===")
rows3 = conn.run("""
    SELECT ROUND(COALESCE(SUM(cost_usd_estimate), 0), 6) AS cost_last_hour
    FROM llm_calls
    WHERE created_at >= NOW() - INTERVAL '1 hour'
""")
for r in rows3:
    print(r)

conn.close()
print("\nDone.", file=sys.stderr)
