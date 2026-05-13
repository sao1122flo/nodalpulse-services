import os, sys, urllib.parse


def main():
    try:
        import pg8000.native as pg
    except ImportError:
        print("pg8000 not installed. Run: .venv\\Scripts\\pip.exe install pg8000")
        sys.exit(1)

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

    with open(sys.argv[1]) as f:
        sql = f.read()

    # Run each semicolon-delimited statement individually.
    # Skips blank chunks and pure-comment chunks (safe for DDL-only files).
    for chunk in sql.split(";"):
        stmt = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if stmt:
            conn.run(stmt)

    conn.close()
    print(f"Applied: {sys.argv[1]}")


main()
