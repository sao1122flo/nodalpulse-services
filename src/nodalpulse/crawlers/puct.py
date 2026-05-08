"""PUCT Interchange crawler — scrapes new filings by date range.

Three-level architecture mirroring the new ASP.NET MVC site:
  GET /search/search/    → redirects to /search/dockets/  (dockets active in range)
  GET /search/filings/   (filing items per docket, date-filtered)
  GET /search/documents/ (downloadable files per item)

Dedup: after L1+L2 resolves (control_number, item_number) pairs, existing
items are filtered out before L3 document-URL fetches fire — cuts ~95% of
L3 requests on steady-state nightly runs.
"""

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import BaseCrawler, RawFiling
from nodalpulse.db.filings import get_existing_item_keys, get_source_id

logger = logging.getLogger(__name__)

BASE_URL = "https://interchange.puc.texas.gov"
SEARCH_URL = f"{BASE_URL}/search/search/"
FILINGS_URL = f"{BASE_URL}/search/filings/"
DOCUMENTS_URL = f"{BASE_URL}/search/documents/"

_CHICAGO = ZoneInfo("America/Chicago")
_CONCURRENCY = 5  # max parallel HTTP requests per crawl phase

_DOC_TYPE_MAP: dict[str, str] = {
    "order": "puct-order",
    "emergency order": "puct-order",
    "preliminary order": "puct-order",
    "final order": "puct-order",
    "notice": "puct-notice",
    "notice of filing": "puct-notice",
    "notice of application": "puct-notice",
    "application": "puct-filing",
    "amended application": "puct-filing",
    "motion": "puct-filing",
    "response": "puct-filing",
    "comments": "puct-filing",
    "request": "puct-filing",
    "petition": "puct-filing",
    "complaint": "puct-filing",
    "rule": "puct-filing",
    "proposed rule": "puct-filing",
}


class PuctCrawler(BaseCrawler):
    source_slug = "puct"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        # Kept for interface compliance; handler uses get_rows() + per-file download instead.
        raise NotImplementedError("Use get_rows() + _download_filing() to avoid OOM")

    async def get_rows(self, since: str | None = None) -> list[dict]:
        """L1+L2+dedup+L3: return rows with doc_url resolved, no content downloaded."""
        since_date = date.fromisoformat(since) if since else date.today() - timedelta(days=1)
        until_date = date.today()
        logger.info("PUCT crawl: %s → %s", since_date, until_date)

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            verify=False,
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            all_items = await self._fetch_filing_items(client, since_date, until_date)
            logger.info("PUCT: %d filing items in range", len(all_items))

            source_id = await get_source_id(self.source_slug)
            if source_id and all_items:
                item_keys = [i["item_key"] for i in all_items]
                existing = await get_existing_item_keys(source_id, item_keys)
                new_items = [i for i in all_items if i["item_key"] not in existing]
                logger.info("PUCT: %d new items (%d already in DB)", len(new_items), len(existing))
            else:
                new_items = all_items

            rows = await self._fetch_document_urls(client, new_items)
            logger.info("PUCT: %d documents to process", len(rows))
            return rows

    async def _fetch_filing_items(
        self, client: httpx.AsyncClient, since: date, until: date
    ) -> list[dict]:
        """L1: search → docket list. L2: per-docket filing items."""
        resp = await client.get(SEARCH_URL, params={
            "DateFiledFrom": since.isoformat(),
            "DateFiledTo": until.isoformat(),
        })
        resp.raise_for_status()
        dockets = _parse_docket_results(resp.text)
        logger.info("PUCT: %d dockets in range", len(dockets))

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _fetch_items(control_number: str) -> list[dict]:
            async with sem:
                r = await client.get(FILINGS_URL, params={
                    "ControlNumber": control_number,
                    "DateFiledFrom": since.isoformat(),
                    "DateFiledTo": until.isoformat(),
                    "ItemMatch": "0",
                })
                r.raise_for_status()
                items = _parse_filing_results(r.text, control_number)
                # Defensive client-side date filter in case server ignores date params
                return [
                    i for i in items
                    if i["filed_at"] >= since.isoformat()
                ]

        item_lists = await asyncio.gather(*[_fetch_items(d["control_number"]) for d in dockets])
        return [item for sublist in item_lists for item in sublist]

    async def _fetch_document_urls(
        self, client: httpx.AsyncClient, items: list[dict]
    ) -> list[dict]:
        """L3: per-item documents page → rows with doc_url set."""
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _fetch_docs(item: dict) -> list[dict]:
            async with sem:
                r = await client.get(DOCUMENTS_URL, params={
                    "controlNumber": item["control_number"],
                    "itemNumber": item["item_number"],
                })
                r.raise_for_status()
                return [
                    {**item, "doc_url": url, "external_id": _doc_id_from_url(url)}
                    for url in _parse_document_results(r.text)
                ]

        doc_lists = await asyncio.gather(*[_fetch_docs(item) for item in items])
        return [doc for sublist in doc_lists for doc in sublist]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _download_filing(self, client: httpx.AsyncClient, row: dict) -> RawFiling | None:
        doc_url = row.get("doc_url")
        if not doc_url:
            return None
        resp = await client.get(doc_url)
        resp.raise_for_status()
        return RawFiling(
            source_slug=self.source_slug,
            external_id=row["external_id"],
            doc_type=row["doc_type"],
            title=row["title"],
            source_url=doc_url,
            filed_at=row["filed_at"],
            content=resp.content,
            file_ext=_ext_from_response(resp),
            metadata={
                "control_number": row.get("control_number", ""),
                "item_number": row.get("item_number", ""),
                "item_key": row.get("item_key", ""),
                "item_type": row.get("item_type", ""),
                "item_type_raw": row.get("item_type", ""),
                "description_raw": row.get("description_raw", ""),
                "party": row.get("party", ""),
            },
        )


# ── parsing helpers ───────────────────────────────────────────────────────────


def _cell_text(cell) -> str:
    """Normalize cell text: collapse whitespace and non-breaking spaces."""
    raw = cell.text(strip=True).replace("\xa0", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _parse_docket_results(html: str) -> list[dict]:
    """Parse /search/dockets/ → [{control_number, party, description}]."""
    tree = HTMLParser(html)
    table = tree.css_first("table")
    if not table:
        logger.warning("PUCT: dockets table not found")
        return []
    results = []
    for tr in table.css("tr")[1:]:
        cells = tr.css("td")
        if not cells:
            continue
        control_number = _cell_text(cells[0])
        if not control_number:
            continue
        results.append({
            "control_number": control_number,
            "party": _cell_text(cells[2]) if len(cells) > 2 else "",
            "description": _cell_text(cells[3]) if len(cells) > 3 else "",
        })
    return results


def _parse_filing_results(html: str, control_number: str) -> list[dict]:
    """Parse /search/filings/ → [{control_number, item_number, item_key, filed_at, ...}]."""
    tree = HTMLParser(html)
    table = tree.css_first("table")
    if not table:
        return []
    results = []
    for tr in table.css("tr")[1:]:
        cells = tr.css("td")
        if len(cells) < 4:
            continue
        item_number = _cell_text(cells[0])
        filed_raw = _cell_text(cells[1])
        party = _cell_text(cells[2])
        item_type = _cell_text(cells[3])
        description = _cell_text(cells[4]) if len(cells) > 4 else ""

        filed_at = _parse_date(filed_raw)
        if not filed_at or not item_number:
            continue

        type_lower = description.lower()
        doc_type = next((v for k, v in _DOC_TYPE_MAP.items() if k in type_lower), "puct-filing")
        title = f"{description} — {control_number}" if description else f"Item {item_number} — {control_number}"

        results.append({
            "control_number": control_number,
            "item_number": item_number,
            "item_key": f"{control_number}_{item_number}",
            "filed_at": filed_at,
            "party": party,
            "item_type": item_type,
            "description_raw": description,
            "doc_type": doc_type,
            "title": title,
        })
    return results


def _parse_document_results(html: str) -> list[str]:
    """Parse /search/documents/ → list of absolute PDF download URLs."""
    tree = HTMLParser(html)
    table = tree.css_first("table")
    if not table:
        return []
    urls = []
    for a in table.css("a[href]"):
        href = a.attrs.get("href", "")
        if not href:
            continue
        if href.startswith("http"):
            urls.append(href)
        elif "/Documents/" in href:
            urls.append(urljoin(BASE_URL, href))
    return urls


def _doc_id_from_url(url: str) -> str:
    """Extract document ID from .../Documents/56896_1_1415464.PDF → '56896_1_1415464'."""
    m = re.search(r"/Documents/([^/?#]+?)(?:\.[A-Za-z]{2,4})?$", url)
    return m.group(1) if m else re.sub(r"\W+", "-", url)[-64:]


def _parse_date(raw: str) -> str | None:
    """Parse PUCT date string (Central time) → UTC ISO-8601.
    Accepts M/D/YYYY (live site format), MM/DD/YYYY, and YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            return naive.replace(tzinfo=_CHICAGO).astimezone(UTC).isoformat()
        except ValueError:
            continue
    return None


def _ext_from_response(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "")
    if "pdf" in ct:
        return "pdf"
    if "html" in ct:
        return "html"
    if "word" in ct or "docx" in ct:
        return "docx"
    m = re.search(r"\.(\w{2,4})(?:\?|$)", str(resp.url))
    return m.group(1).lower() if m else "bin"
