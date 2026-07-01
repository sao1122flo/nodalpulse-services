"""probe-source: a read-only diagnostic job that fetches a URL from the worker's
egress and logs its structure. Built because some portals (e.g. VA SCC CloudFront)
geo-restrict to the US, so they can't be reverse-engineered from a dev machine
outside the US — but the Railway worker egresses from the US.

Enqueue:
    INSERT INTO jobs (kind, payload) VALUES ('probe-source',
      '{"url": "https://www.scc.virginia.gov/docketsearch", "method": "GET"}'::jsonb);
    -- or a POST: {"url": "...", "method": "POST", "data": {"field": "value"}, "referer": "..."}
Then read the worker logs (grep "PROBE").
"""

import logging
import re

import httpx
from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def _persist(url: str, status: int, ct: str, body: str) -> None:
    """Store the full fetched body in a probe_results table so it can be read
    directly via SQL — Railway drops log lines above 500/sec, which truncates
    the chunked dumps when a crawl is logging concurrently."""
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS probe_results ("
                    "id bigserial PRIMARY KEY, url text, status int, "
                    "content_type text, body text, fetched_at timestamptz DEFAULT now())"
                )
            )
            await s.execute(
                text(
                    "INSERT INTO probe_results (url, status, content_type, body) "
                    "VALUES (:u, :s, :c, :b)"
                ),
                {"u": url, "s": status, "c": ct, "b": body[:2_000_000]},
            )
            await s.commit()
        logger.info("PROBE: persisted %d bytes to probe_results for %s", len(body), url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PROBE: persist failed: %s: %s", type(exc).__name__, exc)


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _dump_text(text: str, label: str = "JS") -> None:
    """Dump full JS/JSON/text content in numbered chunks so a single probe
    captures a whole SPA module (the HTML _analyze() snippet is too short)."""
    logger.info("PROBE %s: bytes=%d", label, len(text))
    # surface likely API call sites first for quick scanning
    for pat in (
        r'(?:url|baseUrl|apiUrl|endpoint)\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?:get|post|ajax|fetch|http\.(?:get|post))\s*\(\s*["\']([^"\']+)["\']',
        r'moduleId\s*:\s*["\']([^"\']+)["\']',
        r'route\s*:\s*["\']([^"\']*)["\']',
        r'["\'](/?[A-Za-z0-9_./-]*(?:api|svc|ashx|asmx|Search|Docket|Case)[A-Za-z0-9_./-]*)["\']',
    ):
        found = re.findall(pat, text, re.I)[:12]
        if found:
            logger.info("PROBE %s match %s -> %s", label, pat[:26], found)
    # short head in chunks (full body lives in probe_results table)
    chunk = 1400
    for i in range(0, min(len(text), chunk * 3), chunk):
        logger.info("PROBE %s[%04d]: %s", label, i, text[i : i + chunk])


def _analyze(html: str) -> None:
    logger.info("PROBE: bytes=%d", len(html))
    logger.info("PROBE: webforms=%s axd=%s", "__VIEWSTATE" in html, ".axd" in html)
    for m in re.findall(r"<form\b[^>]*>", html)[:3]:
        logger.info("PROBE form: %s", m[:200])
    # hidden inputs (viewstate etc.)
    for m in re.findall(r'<input[^>]*type="hidden"[^>]*>', html)[:12]:
        nm = re.search(r'name="([^"]*)"', m)
        vl = re.search(r'value="([^"]*)"', m)
        logger.info(
            "PROBE hidden: %s = %s",
            nm.group(1) if nm else "?",
            (vl.group(1)[:30] if vl else "")[:30],
        )
    # all named inputs (type/name/value)
    for m in re.findall(r"<input\b[^>]*>", html)[:40]:
        ty = re.search(r'type="([^"]*)"', m)
        nm = re.search(r'name="([^"]*)"', m)
        if nm:
            logger.info("PROBE input: [%s] %s", ty.group(1) if ty else "?", nm.group(1))
    # selects + options
    for sel in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S)[:8]:
        opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', sel[1], re.S)[:8]
        logger.info(
            "PROBE select %s: %s",
            sel[0],
            [(v, re.sub(r"<[^>]+>", "", t).strip()[:20]) for v, t in opts],
        )
    # results-ish markers + doc link patterns
    for pat in (
        r'id="([^"]*[Rr]esult[^"]*)"',
        r'id="([^"]*[Gg]rid[^"]*)"',
        r'href="([^"]*DOCS[^"]*)"',
        r'href="([^"]*\.PDF[^"]*)"',
        r"(PUR-\d{4}-\d{5})",
        r"__doPostBack\(&#39;([^&]+)&#39;",
    ):
        found = re.findall(pat, html)[:5]
        if found:
            logger.info("PROBE match %s -> %s", pat[:22], found)
    # SPA / API discovery: scripts, app roots, api hints, links
    for s in re.findall(r'<script[^>]*src="([^"]+)"', html)[:12]:
        logger.info("PROBE script: %s", s[:120])
    for fw in ("ng-version", "ng-app", "data-reactroot", "__NEXT_DATA__", "window.__", "vue"):
        if fw in html:
            logger.info("PROBE framework-marker: %s", fw)
    for api in re.findall(
        r'["\'](/[A-Za-z0-9_./-]*(?:api|json|svc|ashx|search|docket|case)[A-Za-z0-9_./-]*)["\']',
        html,
        re.I,
    )[:15]:
        logger.info("PROBE api-hint: %s", api[:120])
    for href in re.findall(r'href="(/[^"]+|https?://[^"]+)"', html)[:18]:
        logger.info("PROBE href: %s", href[:120])
    body = re.sub(r"<[^>]+>", " ", html)
    logger.info("PROBE body-snippet: %s", re.sub(r"\s+", " ", body).strip()[:700])


async def handle_probe_source(payload: dict) -> dict:
    url = payload["url"]
    method = (payload.get("method") or "GET").upper()
    # verify=False for sources with a broken cert chain (e.g. PUCT Interchange, which
    # the crawler also fetches with verify=False). Opt-in per probe via "insecure": true.
    verify = not payload.get("insecure")
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    logger.info("PROBE: %s %s (verify=%s)", method, url, verify)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=45, headers=headers, verify=verify
    ) as client:
        try:
            if method == "POST":
                ph = {"Origin": payload.get("origin", ""), "Referer": payload.get("referer", url)}
                r = await client.post(
                    url, data=payload.get("data", {}), headers={k: v for k, v in ph.items() if v}
                )
            else:
                r = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PROBE: request failed: %s: %s", type(exc).__name__, exc)
            return {"ok": False, "error": str(exc)}
        ct = r.headers.get("content-type", "")
        logger.info(
            "PROBE: status=%s final_url=%s server=%s cf-pop=%s ct=%s cookies=%s",
            r.status_code,
            str(r.url),
            r.headers.get("server"),
            r.headers.get("x-amz-cf-pop", ""),
            ct,
            list(r.cookies.keys()),
        )
        await _persist(url, r.status_code, ct, r.text)
        low = url.lower().split("?")[0]
        is_code = (
            low.endswith((".js", ".json", ".mjs"))
            or "javascript" in ct
            or "json" in ct
            or (r.text[:1] in "{[" and "html" not in ct)
        )
        if is_code:
            _dump_text(r.text, "JSON" if "json" in ct or low.endswith(".json") else "JS")
        else:
            _analyze(r.text)
    return {"ok": True, "status": r.status_code}
