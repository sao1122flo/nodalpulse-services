# nodalpulse-services

Python backend for NodalPulse — crawlers, job queue, extraction pipeline, and API.

## Services (Railway)

| Service | URL | Process |
|---|---|---|
| `nodalpulse-services` | `api.nodalpulse.com` | `uvicorn nodalpulse.api.app:app` |
| `nodalpulse-worker` | — | `python -m nodalpulse.worker` |
| `nodalpulse-scheduler` | — | `python -m nodalpulse.cron` |

## API endpoints

```
GET  /health                    liveness check
POST /crawl/puct                enqueue PUCT crawl job
POST /crawl/ercot               enqueue ERCOT crawl job (NPRR + Market Notices)
POST /brief/trigger             enqueue compose-brief jobs for all active users
POST /email/webhooks/brevo      Brevo bounce/complaint webhook
GET  /unsubscribe/{user_id}     one-click unsubscribe landing page
POST /unsubscribe/{user_id}     confirm unsubscribe
```

Crawl endpoints accept an optional JSON body: `{"since": "2026-05-01"}` (ISO date).
If omitted, the crawler defaults to the last 2 days.

## Crawlers

### PUCT
Scrapes recent filings from the PUCT docket system. Uses `httpx` + `selectolax` (server-rendered).

### ERCOT NPRR (`ercot-nprr`)
- **Source:** `https://www.ercot.com/mktrules/issues/reports/nprr/pending`
- Scrapes the pending NPRR listing with Playwright (Incapsula WAF bypass).
- Table columns: `[#] [Title] [Description] [Date Posted] [Sponsor] [Urgent] [Protocol Sections] [Current Status] [Effective Date(s)]`
- Navigates to each NPRR detail page and downloads the first PDF.
- Returns 0 results on most days — new NPRR submissions are infrequent.

### ERCOT Market Notices (`ercot-mn`)
- **Source:** `https://www.ercot.com/services/comm/mkt_notices/archives`
- Scrapes the ~25 most recent market notices with Playwright.
- Table columns: `[Date MM/DD/YYYY] [Notice ID + Subject (linked)]`
- Notice IDs follow the pattern `{prefix}-{category}{MMDDYY}-{seq}` (e.g. `M-A051126-01`).
- ERCOT market notices are **email-format text documents** — no PDF attachments.
  The notice body text is captured directly and stored as `file_ext=txt`.
- Archives page shows ~25 most recent notices; suitable for daily crawling.

## Worker job types

| Job kind | Handler | Description |
|---|---|---|
| `crawl-puct` | `handle_crawl_puct` | Run PUCT crawler |
| `crawl-ercot` | `handle_crawl_ercot` | Run ERCOT NPRR + MN crawlers |
| `extract` | `handle_extract` | Extract structured data from a raw filing |
| `compose-brief` | `handle_compose_brief` | Generate daily brief for one user |

Jobs are stored in Postgres (`jobs` table), retried up to 5 times with 30s × attempt backoff.

## Local development

```bash
uv sync
uv run uvicorn nodalpulse.api.app:app --reload --port 8000
uv run python -m nodalpulse.worker
```

Requires `DATABASE_URL` in environment (or `.env` file).

## Docker

```bash
docker build -t nodalpulse-services .
docker run -e DATABASE_URL=... nodalpulse-services
```

The image installs Playwright Chromium at build time (`PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright`).
The default `CMD` starts the worker; Railway overrides the start command per service.
