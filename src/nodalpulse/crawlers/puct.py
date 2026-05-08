"""PUCT Interchange crawler — scrapes new filings by date range."""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import BaseCrawler, RawFiling

logger = logging.getLogger(__name__)

BASE_URL = "https://interchange.puc.texas.gov"
# PUCT redesigned from ASP.NET WebForms (/Apps/Filings/Home.aspx) to a modern app in 2025
SEARCH_URL = f"{BASE_URL}/search/filings/"

_CHICAGO = ZoneInfo("America/Chicago")

# PUCT filing type label → taxonomy doc-type tag
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
        since_date = date.fromisoformat(since) if since else date.today() - timedelta(days=1)
        until_date = date.today()
        logger.info("PUCT crawl: %s → %s", since_date, until_date)

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            verify=False,  # PUCT uses a gov CA absent from standard bundles
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            rows = await self._search(client, since_date, until_date)
            logger.info("PUCT: found %d rows", len(rows))

            results: list[RawFiling] = []
            for row in rows:
                try:
                    filing = await self._download_filing(client, row)
                    if filing:
                        results.append(filing)
                except Exception:
                    logger.exception("Failed to download filing %s", row.get("external_id", "?"))

            logger.info("PUCT: downloaded %d/%d", len(results), len(rows))
            return results

    async def _search(
        self,
        client: httpx.AsyncClient,
        since: date,
        until: date,
    ) -> list[dict]:
        # New PUCT Interchange uses GET with query params (no ViewState/POST).
        # Date param names discovered by inspecting the live search form — update if PUCT changes them.
        params = {
            "FiledFrom": since.strftime("%m/%d/%Y"),
            "FiledTo": until.strftime("%m/%d/%Y"),
            "DocumentType": "ALL",
            "SortBy": "FileStamp",
            "SortOrder": "Descending",
        }
        resp = await client.get(SEARCH_URL, params=params)
        logger.info("PUCT search GET %s → %d", resp.url, resp.status_code)
        # Log enough HTML to diagnose form field names / table structure if needed
        logger.info("PUCT response snippet: %s", resp.text[:4000])
        resp.raise_for_status()
        return _parse_results(resp.text)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _download_filing(self, client: httpx.AsyncClient, row: dict) -> RawFiling | None:
        doc_url = row.get("doc_url")
        if not doc_url:
            return None

        resp = await client.get(doc_url)
        resp.raise_for_status()

        file_ext = _ext_from_response(resp)
        return RawFiling(
            source_slug=self.source_slug,
            external_id=row["external_id"],
            doc_type=row["doc_type"],
            title=row["title"],
            source_url=doc_url,
            filed_at=row["filed_at"],
            content=resp.content,
            file_ext=file_ext,
            metadata={
                "docket": row.get("docket", ""),
                "filer": row.get("filer", ""),
                "volume_index": row.get("volume_index", 0),
                "volume_total": row.get("volume_total", 1),
            },
        )


# ── parsing helpers ───────────────────────────────────────────────────────────


def _cell_text(cell) -> str:
    """Normalize cell text: collapse \xa0 and internal whitespace."""
    raw = cell.text(strip=True).replace("\xa0", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _parse_results(html: str) -> list[dict]:
    tree = HTMLParser(html)
    # Try common GridView id patterns used by PUCT Interchange
    table = (
        tree.css_first("table[id*='grdFilings']")
        or tree.css_first("table[id*='GridView']")
        or tree.css_first("table[id*='grd']")
    )
    if not table:
        logger.warning("PUCT results table not found — selectors may need updating")
        return []

    results = []
    for tr in table.css("tr")[1:]:  # skip header
        cells = tr.css("td")
        if len(cells) < 4:
            continue
        rows = _parse_row(cells)
        results.extend(rows)
    return results


def _parse_row(cells) -> list[dict]:
    """
    Expected PUCT Interchange column order:
      0: Project/Docket number
      1: Filed date (MM/DD/YYYY, Central time)
      2: Filing type / description
      3: Filer name
      4+: Document link(s) — may include multiple volumes

    Returns one dict per document link (multi-volume filings produce multiple entries).
    """
    try:
        docket = _cell_text(cells[0])
        filed_raw = _cell_text(cells[1])
        filing_type_raw = _cell_text(cells[2])
        filer = _cell_text(cells[3]) if len(cells) > 3 else ""

        # Collect ALL document links — PUCT splits multi-volume filings across siblings
        # urljoin handles both /relative and https://absolute hrefs correctly
        doc_links: list[tuple[str, str]] = []
        for cell in cells[4:]:
            for anchor in cell.css("a[href]"):
                href = anchor.attributes.get("href", "")
                if not href:
                    continue
                doc_url = urljoin(BASE_URL, href)
                m = re.search(r"[Dd]ocument[_]?[Ii][Dd]=(\d+)", href)
                external_id = m.group(1) if m else re.sub(r"\W+", "-", href)[-64:]
                doc_links.append((doc_url, external_id))

        if not doc_links:
            return []

        filed_at = _parse_date(filed_raw)
        if not filed_at:
            return []

        type_lower = filing_type_raw.lower()
        doc_type = next((v for k, v in _DOC_TYPE_MAP.items() if k in type_lower), "puct-filing")

        # Normalize docket: PUCT strips leading zeros inconsistently — store without them
        docket_norm = str(int(docket)) if docket.isdigit() else docket

        total_vols = len(doc_links)
        rows = []
        for i, (doc_url, external_id) in enumerate(doc_links):
            vol_suffix = f" (Vol. {i + 1} of {total_vols})" if total_vols > 1 else ""
            rows.append({
                "docket": docket_norm,
                "external_id": external_id,
                "filed_at": filed_at,
                "doc_type": doc_type,
                "title": f"{filing_type_raw} — {docket_norm}{vol_suffix}" if docket_norm else f"{filing_type_raw}{vol_suffix}",
                "filer": filer,
                "doc_url": doc_url,
                "volume_index": i,
                "volume_total": total_vols,
            })
        return rows

    except Exception:
        logger.debug("Failed to parse row", exc_info=True)
        return []


def _parse_date(raw: str) -> str | None:
    """Parse a PUCT date string (Central time) and return as UTC ISO-8601."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            # PUCT dates are midnight Central time — convert to UTC before storing
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
