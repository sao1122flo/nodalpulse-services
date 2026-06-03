"""PJM FERC crawl handler — docket discovery pump + filing ingestion.

Two-phase design per engineering-kickoff-brief.md §T8:

Phase 1 — Discovery: fetch the FERC eCollection RSS firehose and scan for items
where "PJM Interconnection" appears in the title (filer-position, not description,
to avoid false positives from interventions that merely mention PJM in the body).
Any FERC docket IDs found in those titles that are not yet in the PJM watch set
are written to the dockets table with jurisdiction='PJM-FERC'. This keeps the
watch set current without manual maintenance.

Phase 2 — Ingestion: FercAdapter(pjm_docket_set) polls the RSS for the updated
watch set and delegates to run_adapter for persist/R2/junction/extraction.

Two HTTP requests per run (discovery fetch + FercAdapter's ingestion fetch). The
RSS feed is ~50 KB/day. Avoiding the double fetch would require exposing raw items
from FercAdapter, a shared component whose interface is intentionally stable.

Jurisdiction stamp: run_adapter uses _SOURCE_JURISDICTION["pjm"] = "PJM-FERC"
(crawl_shared.py) so every docket created during ingestion is marked PJM-FERC.
Multi-docket captions and filing_dockets junction rows are handled by run_adapter
exactly as for CAISO.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.ferc import FercAdapter
from nodalpulse.db.filings import find_or_create_docket, get_pjm_ferc_docket_set, get_source_id
from nodalpulse.workers.crawl_shared import run_adapter

logger = logging.getLogger(__name__)

_FEED_URL = "https://ecollection.ferc.gov/api/rssfeed"

# Match "PJM Interconnection" in the RSS title field only (not description).
# Covers "PJM Interconnection, L.L.C.", "PJM Interconnection LLC", etc.
_PJM_FILER_RE = re.compile(r"pjm interconnection", re.IGNORECASE)

# FERC docket regex — matches ER/EL/RM/OA and strips sub-docket suffix.
_DOCKET_RE = re.compile(r"\b([A-Z]{1,4}\d{2}-\d+(?:-\d{3})?)\b")
_SUB_DOCKET_RE = re.compile(r"-\d{3}$")

# RSS discovery lookback; aligned with WORKER_MAX_LOOKBACK_DAYS.
_MAX_LOOKBACK_DAYS = int(os.environ.get("WORKER_MAX_LOOKBACK_DAYS", "3"))

# First run / IMM bootstrap: do not scan the entire back-catalog.
_DISCOVERY_FLOOR = date(2025, 1, 1)


def _normalize_docket(d: str) -> str:
    return _SUB_DOCKET_RE.sub("", d.strip().upper())


def _parse_dockets(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for raw in _DOCKET_RE.findall(text):
        seen.setdefault(_normalize_docket(raw), None)
    return list(seen)


def _months_in_range(since: date, until: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _parse_rss_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_rss(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if "feed" in root.tag.lower():
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = []
        for e in root.findall("a:entry", ns):
            link_el = e.find("a:link", ns)
            entries.append({
                "title":    (e.find("a:title", ns) or ET.Element("x")).text or "",
                "link":     link_el.get("href", "") if link_el is not None else "",
                "pub_date": (e.find("a:updated", ns) or e.find("a:published", ns) or ET.Element("x")).text or "",
                "description": (e.find("a:summary", ns) or e.find("a:content", ns) or ET.Element("x")).text or "",
            })
        return entries
    return [
        {
            "title":       (i.find("title") or ET.Element("x")).text or "",
            "link":        (i.find("link") or ET.Element("x")).text or "",
            "pub_date":    (i.find("pubDate") or ET.Element("x")).text or "",
            "description": (i.find("description") or ET.Element("x")).text or "",
        }
        for i in root.findall(".//item")
    ]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_month(client: httpx.AsyncClient, year: int, month: int) -> list[dict]:
    resp = await client.get(_FEED_URL, params={"month": f"{month:02d}", "year": str(year)})
    resp.raise_for_status()
    return _parse_rss(resp.text)


async def _fetch_firehose(since_date: date) -> list[dict]:
    since_floored = max(since_date, _DISCOVERY_FLOOR)
    months = _months_in_range(since_floored, date.today())
    all_items: list[dict] = []
    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
    ) as client:
        for year, month in months:
            all_items.extend(await _fetch_month(client, year, month))
    return all_items


async def _discover_pjm_dockets(
    source_id: str,
    items: list[dict],
    since_date: date,
    known_set: set[str],
) -> int:
    """Scan RSS items for PJM Interconnection filer. Write new dockets to DB.

    Returns count of newly discovered dockets.
    """
    discovered = 0
    for item in items:
        title = item.get("title", "")
        if not _PJM_FILER_RE.search(title):
            continue

        pub_dt = _parse_rss_date(item.get("pub_date", ""))
        if pub_dt and pub_dt.date() < since_date:
            continue

        for docket in _parse_dockets(title):
            if docket not in known_set:
                await find_or_create_docket(source_id, docket, jurisdiction="PJM-FERC")
                known_set.add(docket)
                discovered += 1
                logger.info("crawl_pjm: discovered new docket %s from title %r", docket, title[:80])

    return discovered


async def handle_crawl_pjm(payload: dict) -> dict:
    """Crawl FERC eCollection RSS for PJM-FERC filings.

    Phase 1 — Discovery: scan firehose for PJM Interconnection filer items;
    write new PJM-FERC dockets so the watch set grows automatically.

    Phase 2 — Ingestion: FercAdapter(pjm_set) fetches the RSS filtered to the
    (now-updated) PJM docket watch set and persists filings via run_adapter.
    """
    source_id = await get_source_id("pjm")
    if not source_id:
        logger.warning("handle_crawl_pjm: 'pjm' source row not found — apply seed-pjm-source.sql first")
        return {"source": "pjm", "saved": 0, "skipped": 0, "errors": 0, "watched": 0, "discovered": 0}

    since = payload.get("since")
    since_date = (
        date.fromisoformat(since)
        if since
        else date.today() - timedelta(days=_MAX_LOOKBACK_DAYS)
    )

    # Phase 1 — Discovery (one RSS fetch).
    items = await _fetch_firehose(since_date)
    logger.info("handle_crawl_pjm: discovery fetch returned %d RSS items", len(items))

    known_set = await get_pjm_ferc_docket_set()
    discovered = await _discover_pjm_dockets(source_id, items, since_date, known_set)
    if discovered:
        logger.info("handle_crawl_pjm: discovered %d new PJM-FERC docket(s)", discovered)

    # Phase 2 — Ingestion (FercAdapter fetches RSS again; ~50 KB, negligible).
    pjm_set = await get_pjm_ferc_docket_set()
    if not pjm_set:
        logger.warning(
            "handle_crawl_pjm: PJM docket set empty after discovery — "
            "apply seed-pjm-dockets.sql to bootstrap"
        )
        return {"source": "pjm", "saved": 0, "skipped": 0, "errors": 0, "watched": 0, "discovered": discovered}

    result = await run_adapter(FercAdapter(pjm_set), "pjm", since)
    result["discovered"] = discovered
    result["watched"] = len(pjm_set)
    logger.info("handle_crawl_pjm complete: %s", result)
    return result
