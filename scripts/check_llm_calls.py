"""Spot-check script for the llm_calls table.

Usage:
    railway run --service nodalpulse-services .venv\\Scripts\\python.exe scripts/check_llm_calls.py
    railway run --service nodalpulse-services .venv\\Scripts\\python.exe scripts/check_llm_calls.py --limit 10

Prints the last N rows (default 5) plus a per-stage summary.
Run twice on the same filing to verify cache_read_input_tokens > 0 on the second run.
"""

import os, sys, urllib.parse


def main() -> None:
    try:
        import pg8000.native as pg
    except ImportError:
        print("pg8000 not installed. Run: .venv\\Scripts\\pip.exe install pg8000")
        sys.exit(1)

    limit = 5
    args = sys.argv[1:]
    if "--limit" in args:
        try:
            limit = int(args[args.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    # DATABASE_PUBLIC_URL is the externally-reachable proxy — required when running
    # locally via `railway run` because DATABASE_URL resolves only inside Railway's
    # private network (postgres.railway.internal).
    raw = os.environ.get("DATABASE_PUBLIC_URL") or os.environ["DATABASE_URL"]
    for prefix in ("postgresql+asyncpg://", "postgres://"):
        raw = raw.replace(prefix, "postgresql://")
    p = urllib.parse.urlparse(raw)

    conn = pg.Connection(
        host=p.hostname,
        port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        ssl_context=True,
    )

    # ── row count ─────────────────────────────────────────────────────────────
    total = conn.run("SELECT COUNT(*) FROM llm_calls")[0][0]
    print(f"\n{'='*70}")
    print(f"  llm_calls total rows: {total}")
    print(f"{'='*70}\n")

    if total == 0:
        print("Table is empty — migration applied but no pipeline runs yet.")
        conn.close()
        return

    # ── recent rows ───────────────────────────────────────────────────────────
    cols = [
        "created_at", "pipeline_stage", "model", "pricing_version",
        "environment", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "cost_usd_estimate", "latency_ms", "request_id",
        "filing_id", "user_id", "error",
    ]
    rows = conn.run(
        f"SELECT {', '.join(cols)} FROM llm_calls ORDER BY created_at DESC LIMIT :limit",
        limit=limit,
    )

    print(f"Last {len(rows)} rows (newest first):\n")
    for r in rows:
        row = dict(zip(cols, r))
        print(f"  created_at   : {row['created_at']}")
        print(f"  stage        : {row['pipeline_stage']}")
        print(f"  model        : {row['model']}")
        print(f"  pricing_ver  : {row['pricing_version']}")
        print(f"  environment  : {row['environment']}")
        print(
            f"  tokens in/out/cache_r/cache_w : "
            f"{row['input_tokens']} / {row['output_tokens']} / "
            f"{row['cache_read_input_tokens']} / {row['cache_creation_input_tokens']}"
        )
        print(f"  cost_usd     : {row['cost_usd_estimate']}")
        print(f"  latency_ms   : {row['latency_ms']}")
        print(f"  request_id   : {row['request_id']}")
        print(f"  filing_id    : {row['filing_id']}")
        print(f"  user_id      : {row['user_id']}")
        print(f"  error        : {row['error']}")
        print()

    # ── per-stage summary (last 24 h) ─────────────────────────────────────────
    scols = ["pipeline_stage", "model", "calls", "input_tokens",
             "output_tokens", "cache_read", "cost_usd", "avg_latency_ms", "errors"]
    summary = conn.run("""
        SELECT pipeline_stage,
               model,
               COUNT(*)::int                                    AS calls,
               SUM(input_tokens)::int                          AS input_tokens,
               SUM(output_tokens)::int                         AS output_tokens,
               SUM(cache_read_input_tokens)::int               AS cache_read,
               ROUND(SUM(cost_usd_estimate), 6)                AS cost_usd,
               ROUND(AVG(latency_ms))::int                     AS avg_latency_ms,
               COUNT(*) FILTER (WHERE error IS NOT NULL)::int  AS errors
        FROM   llm_calls
        WHERE  created_at >= NOW() - INTERVAL '24 hours'
        GROUP  BY pipeline_stage, model
        ORDER  BY pipeline_stage, model
    """)

    if summary:
        print(f"{'─'*70}")
        print("  Per-stage summary — last 24 h:\n")
        for r in summary:
            s = dict(zip(scols, r))
            print(f"  [{s['pipeline_stage']}]  model={s['model']}")
            print(f"    calls={s['calls']}  errors={s['errors']}")
            print(f"    tokens  in={s['input_tokens']}  out={s['output_tokens']}  cache_read={s['cache_read']}")
            print(f"    cost=${s['cost_usd']}  avg_latency={s['avg_latency_ms']} ms")
            print()

    conn.close()


main()
