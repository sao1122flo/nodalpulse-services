"""FERC eLibrary adapter — shared FERC layer for CAISO + PJM.

Polls the eCollection RSS firehose (ecollection.ferc.gov/api/rssfeed), filters
by a watched set of docket numbers, and emits normalized RawFiling objects.

Design decisions (engineering-kickoff-brief.md §T2):
- Feed is all-FERC (~650 items/month cap); filtering is client-side by docket set.
- Multi-docket captions (e.g. ER23-2309, ER24-1394, EL26-34) → all docket IDs
  captured in metadata["docket_numbers"]. run_adapter creates docket rows for all
  of them; filings.docket_id links to the primary. T4 adds the junction rows.
- Sub-docket suffix (-000/-001) is normalized away for matching; the raw title
  is preserved in metadata so extraction can inspect sub-docket context.
- Content is NOT downloaded at crawl time. source_url is persisted; R2 upload
  happens at extraction time (deferred per the R2 free-tier decision).
- Reused by PJM in Week 3 without modification — this is the shared FERC layer.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_FEED_URL = "https://ecollection.ferc.gov/api/rssfeed"

# FERC docket ID: 1-4 uppercase letter prefix, 2-digit year, sequence, optional 3-digit sub-docket.
# Covers ER (rates/tariff), EL (complaints), RM (rulemaking), OA (orders applicable), and others.
_DOCKET_RE = re.compile(r"\b([A-Z]{1,4}\d{2}-\d+(?:-\d{3})?)\b")

# Sub-docket suffix to strip for base-docket normalization: ER23-2309-000 → ER23-2309
_SUB_DOCKET_RE = re.compile(r"-\d{3}$")

# FERC accession number: 14-digit timestamp (YYYYMMDDHHMMSS) in URLs / GUIDs
_ACCESSION_RE = re.compile(r"\d{14}")

# Ordered map: longest/most-specific key first so "request for rehearing" beats "request"
_DOC_TYPE_MAP: dict[str, str] = {
    "tariff amendment": "ferc-tariff-amendment",
    "request for rehearing": "ferc-rehearing",
    "compliance filing": "ferc-compliance",
    "motion to intervene": "ferc-motion",
    "deficiency response": "ferc-response",
    "informational filing": "ferc-informational",
    "notice of cancellation": "ferc-cancellation",
    "notice of termination": "ferc-cancellation",
    "petition for waiver": "ferc-petition",
    "compliance": "ferc-compliance",
    "rehearing": "ferc-rehearing",
    "protest": "ferc-protest",
    "answer": "ferc-answer",
    "motion": "ferc-motion",
    "petition": "ferc-petition",
    "waiver": "ferc-petition",
    "agreement": "ferc-agreement",
    "certificate": "ferc-certificate",
    "report": "ferc-informational",
    "cancellation": "ferc-cancellation",
    "notice": "ferc-notice",
}


class FercAdapter(MarketAdapter):
    """Shared FERC eLibrary adapter. Used by CAISO (Week 1) and PJM (Week 3).

    Args:
        docket_numbers: Base FERC docket IDs to watch, e.g. {"ER23-2309", "EL26-34"}.
                        Sub-docket suffixes (-000/-001) are normalized away before matching.
    """

    source_slug = "ferc"

    def __init__(self, docket_numbers: set[str]) -> None:
        self._watched: set[str] = {_normalize_docket(d) for d in docket_numbers}

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )

        if not self._watched:
            logger.info("FercAdapter: watch set empty — no dockets to poll")
            return []

        logger.info("FercAdapter: polling %d dockets since=%s", len(self._watched), since_date)

        all_items: list[dict] = []
        months = _months_in_range(since_date, date.today())
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"}
        ) as client:
            for year, month in months:
                items = await _fetch_feed(client, year, month)
                all_items.extend(items)

        logger.info(
            "FercAdapter: RSS returned %d raw items across %d month(s)", len(all_items), len(months)
        )

        filings: list[RawFiling] = []
        seen_ids: set[str] = set()
        for item in all_items:
            filing = self._item_to_filing(item, since_date)
            if filing and filing.external_id not in seen_ids:
                filings.append(filing)
                seen_ids.add(filing.external_id)

        logger.info("FercAdapter: %d filings match watched dockets", len(filings))
        return filings

    def _item_to_filing(self, item: dict, since_date: date) -> RawFiling | None:
        """Convert RSS item → RawFiling, or None if outside date range / no watched docket."""
        pub_dt = _parse_rss_date(item.get("pub_date", ""))
        if not pub_dt:
            return None
        if pub_dt.date() < since_date:
            return None

        title = item.get("title", "")
        description = item.get("description", "")
        docket_numbers = _parse_dockets(f"{title} {description}")

        if not (self._watched & set(docket_numbers)):
            return None  # no watched docket in this caption

        external_id = _make_external_id(item)
        source_url = item.get("link") or item.get("guid", "")

        return RawFiling(
            source_slug="ferc",
            external_id=external_id,
            doc_type=_infer_doc_type(title),
            title=title,
            source_url=source_url,
            filed_at=pub_dt.isoformat(),
            content=b"",  # deferred — R2 upload happens at extraction time, not crawl time
            file_ext="pdf",
            metadata={
                "docket_numbers": docket_numbers,  # list — run_adapter links ALL of them
                "raw_title": title,
                "description": description,
                "guid": item.get("guid", ""),
            },
        )


# ── RSS fetch + parse ─────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_feed(client: httpx.AsyncClient, year: int, month: int) -> list[dict]:
    params = {"month": f"{month:02d}", "year": str(year)}
    logger.debug("FercAdapter: fetching RSS year=%d month=%02d", year, month)
    resp = await client.get(_FEED_URL, params=params)
    resp.raise_for_status()
    return _parse_rss(resp.text)


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse RSS 2.0 or Atom feed → list of normalized item dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("FercAdapter: XML parse error: %s", exc)
        return []

    if "feed" in root.tag.lower():
        # Atom 1.0
        ns = {"a": "http://www.w3.org/2005/Atom"}
        return [_parse_atom_entry(e, ns) for e in root.findall("a:entry", ns)]

    # RSS 2.0 — items nested under channel
    return [_parse_rss_item(i) for i in root.findall(".//item")]


def _parse_rss_item(item: ET.Element) -> dict:
    def text(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None else ""

    return {
        "title": text("title"),
        "link": text("link"),
        "pub_date": text("pubDate"),
        "description": text("description"),
        "guid": text("guid"),
    }


def _parse_atom_entry(entry: ET.Element, ns: dict) -> dict:
    def text(tag: str) -> str:
        el = entry.find(tag, ns)
        return (el.text or "").strip() if el is not None else ""

    link_el = entry.find("a:link", ns)
    link = link_el.get("href", "") if link_el is not None else ""

    return {
        "title": text("a:title"),
        "link": link,
        "pub_date": text("a:updated") or text("a:published"),
        "description": text("a:summary") or text("a:content"),
        "guid": text("a:id"),
    }


# ── helpers ───────────────────────────────────────────────────────────────────


def _normalize_docket(docket: str) -> str:
    """Strip -000/-001 sub-docket suffix → base docket ID for matching."""
    return _SUB_DOCKET_RE.sub("", docket.strip().upper())


def _parse_dockets(text: str) -> list[str]:
    """Extract all FERC docket IDs from text, deduplicated and normalized to base dockets.

    Preserves order of first occurrence so the primary captioned docket is first.
    Example: "ER23-2309, ER24-1394, EL26-34 DCR Transmission" → ["ER23-2309", "ER24-1394", "EL26-34"]
    """
    seen: dict[str, None] = {}  # insertion-ordered set
    for raw in _DOCKET_RE.findall(text):
        seen.setdefault(_normalize_docket(raw), None)
    return list(seen)


def _make_external_id(item: dict) -> str:
    """Derive a stable, unique external_id from an RSS item.

    Prefers the 14-digit FERC accession number found in GUID or link URL.
    Falls back to a SHA-1 hash of the link URL for items with non-standard GUIDs.
    """
    for candidate in (item.get("guid", ""), item.get("link", "")):
        m = _ACCESSION_RE.search(candidate)
        if m:
            return m.group(0)
    raw = (item.get("link") or item.get("guid") or item.get("title", "")).encode()
    return "ferc-" + hashlib.sha1(raw).hexdigest()[:16]


def _parse_rss_date(raw: str) -> datetime | None:
    """Parse RSS pubDate or Atom updated → UTC datetime."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            return naive.replace(tzinfo=UTC)
        except ValueError:
            continue
    logger.warning("FercAdapter: unparseable date %r", raw)
    return None


def _infer_doc_type(title: str) -> str:
    """Infer FERC filing type from title via longest-match. Defaults to ferc-filing."""
    lower = title.lower()
    for key, doc_type in _DOC_TYPE_MAP.items():
        if key in lower:
            return doc_type
    return "ferc-filing"


def _months_in_range(since: date, until: date) -> list[tuple[int, int]]:
    """Return (year, month) pairs covering since..until inclusive."""
    months: list[tuple[int, int]] = []
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months
