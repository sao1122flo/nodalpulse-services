import asyncio
import logging
import re

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from nodalpulse.queue.pg_queue import enqueue

logger = logging.getLogger(__name__)
app = FastAPI(title="nodalpulse-services", version="0.1.0")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


class CrawlRequest(BaseModel):
    since: str | None = None  # ISO date, e.g. "2026-05-01"; defaults to last crawled date


@app.post("/crawl/puct")
async def trigger_crawl_puct(body: CrawlRequest | None = None) -> JSONResponse:
    if body is None:
        body = CrawlRequest()
    job_id = await enqueue("crawl-puct", {"since": body.since}, priority=10)
    logger.info("Enqueued crawl-puct job %s (since=%s)", job_id, body.since)
    return JSONResponse({"job_id": job_id, "status": "queued"})


_PUCT_BASE = "https://interchange.puc.texas.gov"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_CANDIDATES = [
    "/api/filings",
    "/api/v1/filings",
    "/api/search/filings",
    "/api/v1/search/filings",
    "/search/api/filings",
    "/_api/filings",
    "/api/filings/search",
    "/odata/filings",
    "/graphql",
]
_SPA_PATTERNS = [
    "__NEXT_DATA__", "__INITIAL_STATE__", "__APOLLO_STATE__",
    '<div id="app">', '<div id="root">', "data-react", "ng-app",
]


@app.get("/discover/puct")
async def discover_puct() -> JSONResponse:
    """Temporary endpoint: probe the PUCT Interchange site from Railway's US IP
    to find the backing JSON API before committing to a scraping strategy."""
    async with httpx.AsyncClient(
        headers=_BROWSER_HEADERS, follow_redirects=True, timeout=30, verify=False
    ) as client:
        # 1. Fetch the SPA shell
        r = await client.get(f"{_PUCT_BASE}/search/filings/")
        tree = HTMLParser(r.text)

        spa_found = [p for p in _SPA_PATTERNS if re.search(re.escape(p), r.text)]
        scripts = [s.attrs.get("src", "") for s in tree.css("script[src]")]

        api_hints: list[dict] = []
        for script in tree.css("script"):
            body = script.text() or ""
            for hint in ["api", "endpoint", "graphql", "/v1/", "/v2/", "ApiUrl", "baseUrl"]:
                if hint.lower() in body.lower():
                    idx = body.lower().find(hint.lower())
                    api_hints.append({
                        "hint": hint,
                        "context": body[max(0, idx - 60): idx + 140].strip(),
                    })

        # 2. Probe common API paths in parallel
        async def _probe(path: str) -> dict:
            try:
                pr = await client.get(
                    f"{_PUCT_BASE}{path}",
                    headers={**_BROWSER_HEADERS, "Accept": "application/json"},
                    timeout=10,
                )
                return {
                    "path": path, "status": pr.status_code,
                    "content_type": pr.headers.get("content-type", ""),
                    "snippet": pr.text[:300],
                }
            except Exception as exc:
                return {"path": path, "error": str(exc)}

        probes = await asyncio.gather(*[_probe(p) for p in _API_CANDIDATES])

    return JSONResponse({
        "shell_status": r.status_code,
        "shell_length": len(r.text),
        "spa_patterns_found": spa_found,
        "scripts": scripts,
        "api_hints": api_hints[:10],
        "api_probes": list(probes),
        "shell_snippet": r.text[:1500],
    })
