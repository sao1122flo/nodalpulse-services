"""Temporary diagnostic: fetch FERC RSS from Railway server and report raw contents."""
import logging
import httpx
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_FEED_URL = "https://ecollection.ferc.gov/api/rssfeed"


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Fetch one month of FERC RSS and report what came back, unfiltered."""
    year  = payload.get("year",  2026)
    month = payload.get("month", 3)

    params = {"month": f"{month:02d}", "year": str(year)}
    logger.info("diagnose_ferc: fetching %s params=%s", _FEED_URL, params)

    try:
        async with httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            resp = await client.get(_FEED_URL, params=params)
            status = resp.status_code
            body_len = len(resp.content)
            content_type = resp.headers.get("content-type", "?")
            body_preview = resp.text[:500]

            logger.info("diagnose_ferc: status=%d  len=%d  ct=%s", status, body_len, content_type)
    except Exception as exc:
        logger.error("diagnose_ferc: HTTP error: %s", exc)
        return {"error": str(exc), "year": year, "month": month}

    # Try to parse RSS
    try:
        root = ET.fromstring(resp.text)
        items = root.findall(".//item")
        item_count = len(items)
        # Grab first 5 titles to see if ER dockets appear
        titles = []
        for item in items[:5]:
            t = (item.find("title") or ET.Element("x")).text or ""
            titles.append(t[:120])
        logger.info("diagnose_ferc: parsed %d items, first titles: %s", item_count, titles)
    except ET.ParseError as exc:
        item_count = -1
        titles = []
        logger.warning("diagnose_ferc: XML parse error: %s  body[:200]=%r", exc, body_preview[:200])

    return {
        "year":         year,
        "month":        month,
        "http_status":  status,
        "body_len":     body_len,
        "content_type": content_type,
        "item_count":   item_count,
        "first_5_titles": titles,
        "body_preview": body_preview[:300],
    }
