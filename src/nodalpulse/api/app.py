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


def _snapshot_cookies(client: httpx.AsyncClient) -> list[dict]:
    """Return current cookie jar as a list of {name, domain, path} dicts (no values)."""
    return [
        {"name": c.name, "domain": c.domain, "path": c.path}
        for c in client.cookies.jar
    ]


def _dump_form_inputs(tree: HTMLParser) -> tuple[list[dict], list[dict]]:
    """Return (inputs, form_actions) from a parsed page — skips ASP.NET __ fields."""
    inputs = []
    for el in tree.css("input[name], select[name], textarea[name]"):
        name = el.attrs.get("name", "")
        if name.startswith("__"):
            continue
        inputs.append({
            "name": name,
            "type": el.attrs.get("type", el.tag),
            "value": el.attrs.get("value", ""),
            "placeholder": el.attrs.get("placeholder", ""),
        })
    actions = [
        {"action": f.attrs.get("action", ""), "method": f.attrs.get("method", "")}
        for f in tree.css("form")
    ]
    return inputs, actions


def _dump_table_headers(tree: HTMLParser) -> list[dict]:
    """Return column headers for every table that has at least one row."""
    out = []
    for t in tree.css("table"):
        rows = t.css("tr")
        header_row = t.css_first("thead tr") or (rows[0] if rows else None)
        if not header_row:
            continue
        headers = [th.text(strip=True) for th in header_row.css("th, td")]
        if headers:
            out.append({
                "id": t.attrs.get("id", ""),
                "class": t.attrs.get("class", ""),
                "rows": len(rows),
                "headers": headers,
            })
    return out


@app.get("/discover/puct")
async def discover_puct() -> JSONResponse:
    """Temporary endpoint: probe the PUCT Interchange site from Railway's US IP
    to find the backing JSON API before committing to a scraping strategy."""
    async with httpx.AsyncClient(
        headers=_BROWSER_HEADERS, follow_redirects=True, timeout=30, verify=False
    ) as client:
        # 1. Fetch the search form page — extract form inputs to learn accepted param names
        r = await client.get(f"{_PUCT_BASE}/search/filings/")
        cookies_after_shell = _snapshot_cookies(client)
        tree = HTMLParser(r.text)

        spa_found = [p for p in _SPA_PATTERNS if re.search(re.escape(p), r.text)]
        scripts = [s.attrs.get("src", "") for s in tree.css("script[src]")]
        form_inputs, form_actions = _dump_form_inputs(tree)

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

        # 3. Fetch a known-working ControlNumber page — inspect table structure and form inputs
        known = await client.get(f"{_PUCT_BASE}/Search/Filings?ControlNumber=56896")
        cookies_after_known = _snapshot_cookies(client)
        known_tree = HTMLParser(known.text)
        table_headers = _dump_table_headers(known_tree)
        known_form_inputs, known_form_actions = _dump_form_inputs(known_tree)
        table_samples = []
        for t in known_tree.css("table"):
            if len(t.css("tr")) > 1:
                table_samples.append({"id": t.attrs.get("id", ""), "html": t.html[:600]})

        # 4. Try date param name candidates (PascalCase convention, ranked by likelihood)
        date_param_guesses = [
            {"FiledFrom": "05/06/2026", "FiledTo": "05/08/2026"},
            {"DateFiledFrom": "05/06/2026", "DateFiledTo": "05/08/2026"},
            {"FilingDateFrom": "05/06/2026", "FilingDateTo": "05/08/2026"},
            {"DateFrom": "05/06/2026", "DateTo": "05/08/2026"},
            {"StartDate": "05/06/2026", "EndDate": "05/08/2026"},
        ]

        async def _try_date_params(params: dict) -> dict:
            resp = await client.get(f"{_PUCT_BASE}/Search/Filings", params=params)
            t = HTMLParser(resp.text)
            tbls = t.css("table")
            row_counts = [len(tbl.css("tr")) for tbl in tbls]
            return {
                "params": params,
                "status": resp.status_code,
                "tables_found": len(tbls),
                "table_ids": [tbl.attrs.get("id", "") for tbl in tbls],
                "row_counts": row_counts,
                "snippet": resp.text[2000:3000],
            }

        # 5. Probe PageSize to detect pagination cap
        async def _try_pagesize(size: int) -> dict:
            resp = await client.get(
                f"{_PUCT_BASE}/Search/Filings",
                params={"ControlNumber": "56896", "PageSize": size},
            )
            t = HTMLParser(resp.text)
            tbls = t.css("table")
            return {
                "PageSize": size,
                "status": resp.status_code,
                "tables_found": len(tbls),
                "row_counts": [len(tbl.css("tr")) for tbl in tbls],
            }

        date_probes, pagesize_probes = await asyncio.gather(
            asyncio.gather(*[_try_date_params(p) for p in date_param_guesses]),
            asyncio.gather(*[_try_pagesize(s) for s in [10, 50, 100, 200]]),
        )

        # 6. Targeted probe: correct endpoint + correct date format to see docket list rows
        search_resp = await client.get(
            f"{_PUCT_BASE}/search/search/",
            params={"DateFiledFrom": "2026-05-06", "DateFiledTo": "2026-05-08"},
        )
        search_tree = HTMLParser(search_resp.text)
        search_table = search_tree.css_first("table")
        data_row_samples = []
        if search_table:
            for tr in search_table.css("tr")[1:6]:  # first 5 data rows
                data_row_samples.append({
                    "html": tr.html,
                    "links": [
                        {"text": a.text(strip=True), "href": a.attrs.get("href", "")}
                        for a in tr.css("a[href]")
                    ],
                    "cells": [td.text(strip=True) for td in tr.css("td")],
                })

        # 7. Probe the filing-level page for a known docket — reveals document link structure
        filings_resp = await client.get(
            f"{_PUCT_BASE}/search/filings/",
            params={"ControlNumber": "56896", "ItemMatch": "0"},
        )
        filings_tree = HTMLParser(filings_resp.text)
        filings_table = filings_tree.css_first("table")
        filing_row_samples = []
        if filings_table:
            for tr in filings_table.css("tr")[1:6]:  # first 5 data rows
                filing_row_samples.append({
                    "html": tr.html,
                    "links": [
                        {"text": a.text(strip=True), "href": a.attrs.get("href", "")}
                        for a in tr.css("a[href]")
                    ],
                    "cells": [td.text(strip=True) for td in tr.css("td")],
                })

        # 8. Probe the documents page — reveals actual download link URLs
        docs_resp = await client.get(
            f"{_PUCT_BASE}/search/documents/",
            params={"controlNumber": "56896", "itemNumber": "1"},
        )
        docs_tree = HTMLParser(docs_resp.text)
        docs_table = docs_tree.css_first("table")
        doc_row_samples = []
        if docs_table:
            for tr in docs_table.css("tr")[1:6]:
                doc_row_samples.append({
                    "html": tr.html,
                    "links": [
                        {"text": a.text(strip=True), "href": a.attrs.get("href", "")}
                        for a in tr.css("a[href]")
                    ],
                    "cells": [td.text(strip=True) for td in tr.css("td")],
                })
        # Also grab all links on the page in case documents aren't in a table
        all_doc_links = [
            {"text": a.text(strip=True), "href": a.attrs.get("href", "")}
            for a in docs_tree.css("a[href]")
            if any(x in a.attrs.get("href", "") for x in ["document", "download", "file", "pdf", ".pdf", "getdoc"])
        ]

    return JSONResponse({
        "shell_status": r.status_code,
        "shell_length": len(r.text),
        "spa_patterns_found": spa_found,
        "scripts": scripts,
        "form_inputs": form_inputs,
        "form_actions": form_actions,
        "api_hints": api_hints[:5],
        "api_probes": list(probes),
        "known_url_table_headers": table_headers,
        "known_url_table_samples": table_samples,
        "known_url_form_inputs": known_form_inputs,
        "known_url_form_actions": known_form_actions,
        "date_param_probes": list(date_probes),
        "pagesize_probes": list(pagesize_probes),
        "search_probe": {
            "status": search_resp.status_code,
            "url": str(search_resp.url),
            "tables_found": 1 if search_table else 0,
            "total_rows": len(search_table.css("tr")) if search_table else 0,
            "data_row_samples": data_row_samples,
        },
        "filing_level_probe": {
            "status": filings_resp.status_code,
            "url": str(filings_resp.url),
            "total_rows": len(filings_table.css("tr")) if filings_table else 0,
            "filing_row_samples": filing_row_samples,
        },
        "documents_page_probe": {
            "status": docs_resp.status_code,
            "url": str(docs_resp.url),
            "table_found": docs_table is not None,
            "total_rows": len(docs_table.css("tr")) if docs_table else 0,
            "doc_row_samples": doc_row_samples,
            "all_doc_links": all_doc_links[:20],
        },
        "cookies_after_shell": cookies_after_shell,
        "cookies_after_known_url": cookies_after_known,
    })
