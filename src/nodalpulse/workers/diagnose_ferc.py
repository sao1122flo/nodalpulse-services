"""Temporary diagnostic: probe multiple FERC filing RSS/API URLs from Railway server."""
import logging
import httpx
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# Candidate URLs — focusing on FERC eSubscription per-docket RSS pattern
# and FERC full-text search API (EFTS)
_CANDIDATES = [
    # FERC eSubscription — per-docket RSS (classic pattern)
    ("esub-docket-rss",  "https://esub.ferc.gov/rss/docketRss.asp?docket_number=ER26-455"),
    ("esub-root",        "https://esub.ferc.gov/rss/"),
    ("esub-recent",      "https://esub.ferc.gov/rss/recentFilings.asp"),
    # FERC full-text search (EFTS) — might have JSON/RSS output
    ("efts-json",        "https://efts.ferc.gov/EFTS-Java/search.do?q=ER26-455&format=json&rows=5"),
    ("efts-html",        "https://efts.ferc.gov/EFTS-Java/search.do?q=ER26-455&rows=5"),
    # FERC eLibrary docket sheet — HTML but parseable
    ("elibrary-sheet",   "https://elibrary.ferc.gov/eLibrary/docketsheet?docket_number=ER26-455"),
]


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Probe multiple FERC filing URLs and report what each returns."""
    results = {}
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 NodalPulse/1.0 regulatory-monitor"},
    ) as client:
        for name, url in _CANDIDATES:
            try:
                resp = await client.get(url)
                body = resp.text[:600]
                # Check if it's XML/RSS and has docket-like content
                has_er = "ER" in body or "EL" in body or "er2" in body.lower()
                has_rss = "<rss" in body or "<feed" in body or "<channel" in body
                results[name] = {
                    "url":    url,
                    "status": resp.status_code,
                    "len":    len(resp.content),
                    "ct":     resp.headers.get("content-type", "?")[:60],
                    "is_rss": has_rss,
                    "has_er_el": has_er,
                    "preview": body[:300],
                }
                logger.info("diagnose_ferc %s: status=%d len=%d rss=%s er=%s",
                            name, resp.status_code, len(resp.content), has_rss, has_er)
            except Exception as exc:
                results[name] = {"url": url, "error": str(exc)[:120]}
                logger.warning("diagnose_ferc %s: %s", name, exc)

    return results
