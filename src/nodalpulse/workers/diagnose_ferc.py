"""Temporary diagnostic: probe multiple FERC filing RSS/API URLs from Railway server."""
import logging
import httpx
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# Candidate URLs for FERC tariff/docket filing RSS (not XBRL financial reports)
_CANDIDATES = [
    # eFiling API — the new FERC filing submission system
    ("efilingapi-rss",   "https://efilingapi.ferc.gov/efiling/tariffFilings/rss"),
    ("efilingapi-list",  "https://efilingapi.ferc.gov/efiling/tariffFilings"),
    ("efiling-rss",      "https://efiling.ferc.gov/efi/searchDocument.html?format=rss"),
    # eLibrary search — might expose RSS
    ("elibrary-rss",     "https://elibrary.ferc.gov/eLibrary/search?format=rss&q=ER26"),
    ("elibrary-docket",  "https://elibrary.ferc.gov/eLibrary/docketsheet?docket_number=ER26-455&format=rss"),
    # eSubscription feed
    ("esub",             "https://elibrary.ferc.gov/eLibrary/search?dType=FILINGS&dateRange=custom&startDate=03%2F01%2F2026&endDate=04%2F01%2F2026&format=rss"),
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
