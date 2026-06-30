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

logger = logging.getLogger(__name__)

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
    # full body in chunks
    chunk = 1400
    for i in range(0, min(len(text), chunk * 14), chunk):
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
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    logger.info("PROBE: %s %s", method, url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=45, headers=headers) as client:
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
