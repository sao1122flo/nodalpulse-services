"""FERC eLibrarywebapi adapter — shared FERC layer for CAISO + PJM.

Polls https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch
per watched docket and emits normalized RawFiling objects.

Replaces the defunct ecollection.ferc.gov/api/rssfeed (returned XBRL data,
not tariff filings — zero useful hits since the adapter was built).

PDF strategy (uniform for all filers):
- source_url = "" at crawl time (deferred)
- metadata["ferc_file_id"] = transmittals[0].fileId (unique per document)
- At extraction time, extract.py calls POST /File/DownloadP8File (FileNet P8 CMS)
  with {"fileidLst": [ferc_file_id]} after a GET /eLibrary/ for session cookies.
  Confirmed working for PJM filings, FERC orders, and third-party interventions.

Design decisions:
- Multi-docket: docketNumbers[] list deduped after normalization → metadata["docket_numbers"]
- Dedup key: acesssionNumber (FERC API typo, field name preserved) → external_id, YYYYMMDD-NNNN
- Filed cursor: filedDate (MM/DD/YYYY) — not postedDate or issuedDate
- Default sort is filedDate DESC (most recent first); early-stop when page tail < since_date
- FERC dateSearches filter is broken (always 0 with allDates=False); fetch allDates=True
  and apply since_date cutoff in Python via early-stop + _item_to_filing() filter
- Sequential per-docket queries; no parallel calls to FERC servers
- Discovery pump removed — affiliations filter matches service lists, not filer-only;
  new PJM dockets are seeded manually, consistent with all other jurisdictions
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_BASE = "https://elibrary.ferc.gov/eLibrarywebapi/api"
_SEARCH_URL = f"{_BASE}/Search/AdvancedSearch"
_RESULTS_PER_PAGE = 50

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 NodalPulse/1.0 regulatory-monitor",
    "Origin": "https://elibrary.ferc.gov",
    "Referer": "https://elibrary.ferc.gov/",
}

# Sub-docket suffix: ER23-2309-000 → ER23-2309, ER23-2309-001 → ER23-2309.
# Must only match 0-prefixed 3-digit suffixes (000-099) so that dockets whose
# sequence number is 3 digits (e.g. EL24-119) are NOT truncated.
_SUB_DOCKET_RE = re.compile(r"-0\d{2}$")

# Ordered map: most-specific key first so "request for rehearing" beats "rehearing"
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
    "tariff filing": "ferc-tariff-amendment",
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
    "complaint": "ferc-complaint",
    "order": "ferc-order",
}


class FercAdapter(MarketAdapter):
    """Shared FERC eLibrarywebapi adapter. Used by CAISO (crawl-ferc) and PJM (crawl-pjm).

    Args:
        docket_numbers: Base FERC docket IDs to watch, e.g. {"ER23-2309", "EL26-34"}.
                        Sub-docket suffixes (-000/-001) are normalized away before querying.
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

        filings: list[RawFiling] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_HEADERS) as client:
            for docket in sorted(self._watched):
                items = await _fetch_docket(client, docket, since_date)
                for item in items:
                    filing = _item_to_filing(item, since_date)
                    if filing and filing.external_id not in seen_ids:
                        filings.append(filing)
                        seen_ids.add(filing.external_id)

        logger.info("FercAdapter: %d new filings across %d dockets", len(filings), len(self._watched))
        return filings


# ── eLibrarywebapi fetch ──────────────────────────────────────────────────────


_MAX_PAGES_PER_DOCKET = 10  # hard cap; safety valve for very large dockets


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _post_with_retry(client: httpx.AsyncClient, body: dict) -> dict:
    """POST AdvancedSearch with retry on timeout/5xx."""
    resp = await client.post(_SEARCH_URL, content=json.dumps(body))
    resp.raise_for_status()
    return resp.json()


async def _fetch_docket(
    client: httpx.AsyncClient,
    docket: str,
    since_date: date,
) -> list[dict]:
    """Fetch recent filings for one docket via AdvancedSearch (allDates=True), paginated.

    FERC dateSearches is broken (always returns 0 with allDates=False). Workaround:
    fetch with allDates=True. Default sort is filedDate DESC (most recent first —
    confirmed by probe). Early-stop: when the last item in a page is older than
    since_date, all subsequent pages are also older; stop without fetching them.
    _item_to_filing() applies the since_date cutoff as a second gate.
    """
    items: list[dict] = []
    page = 1

    while page <= _MAX_PAGES_PER_DOCKET:
        body = {
            "searchText": "*",
            "searchFullText": True,
            "searchDescription": True,
            "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
            "dateSearches": [],
            "affiliations": [],
            "categories": [],
            "libraries": [],
            "classTypes": [],
            "accessionNumber": None,
            "eFiling": False,
            "resultsPerPage": _RESULTS_PER_PAGE,
            "curPage": page,
            "groupBy": "NONE",
            "sortBy": "",   # default = filedDate DESC
            "allDates": True,
        }

        data = await _post_with_retry(client, body)

        batch = data.get("searchHits") or []  # API returns null for some dockets
        total = data.get("totalHits") or 0
        items.extend(batch)

        logger.info("FercAdapter: docket=%s page=%d got=%d total=%d", docket, page, len(batch), total)

        if len(batch) < _RESULTS_PER_PAGE or len(items) >= total:
            break

        # Early-stop: items are sorted filedDate DESC; if tail of this page is older
        # than since_date, all remaining pages are also older — no need to fetch them.
        last_filed = _parse_filed_date(batch[-1].get("filedDate", "")) if batch else None
        if last_filed and last_filed < since_date:
            logger.info("FercAdapter: docket=%s early-stop at page=%d (tail=%s < since=%s)",
                        docket, page, last_filed, since_date)
            break

        page += 1

    if page > _MAX_PAGES_PER_DOCKET:
        logger.warning("FercAdapter: docket=%s hit page cap (%d)", docket, _MAX_PAGES_PER_DOCKET)

    return items


# ── item → RawFiling ──────────────────────────────────────────────────────────


def _item_to_filing(item: dict, since_date: date) -> RawFiling | None:
    """Convert one eLibrarywebapi searchHit → RawFiling, or None if out of date range."""
    # Field name is FERC's own typo — preserved intentionally
    acc = item.get("acesssionNumber", "")
    if not acc:
        return None

    filed = _parse_filed_date(item.get("filedDate", ""))
    if not filed or filed < since_date:
        return None

    # Normalize and deduplicate: API returns sub-docket suffixes (EL25-49-000, EL25-49-001)
    # which normalize to the same base docket; preserve first-occurrence order.
    _seen: dict[str, None] = {}
    docket_numbers = [
        _seen.setdefault(nd, nd)
        for raw in item.get("docketNumbers", [])
        if (nd := _normalize_docket(raw)) not in _seen
    ]

    description = item.get("description", "")
    filer = _get_author(item)
    transmittals = item.get("transmittals", [])
    ferc_file_id = transmittals[0].get("fileId", "") if transmittals else ""

    return RawFiling(
        source_slug="ferc",
        external_id=acc,
        doc_type=_infer_doc_type(item),
        title=description,
        source_url="",   # deferred — fetch at extraction via ferc_file_id + DownloadP8File
        filed_at=filed.isoformat() + "T00:00:00+00:00",
        content=b"",     # deferred — R2 upload happens after triage at extraction time
        file_ext="pdf",
        metadata={
            "docket_numbers": docket_numbers,
            "raw_title": description,
            "description": description,
            "filer": filer,
            "ferc_file_id": ferc_file_id,
            "ferc_file_name": transmittals[0].get("fileName", "") if transmittals else "",
            "ferc_accession": acc,
        },
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _normalize_docket(docket: str) -> str:
    return _SUB_DOCKET_RE.sub("", docket.strip().upper())


def _parse_filed_date(raw: str) -> date | None:
    """Parse MM/DD/YYYY (FERC API date format) → date."""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def _get_author(item: dict) -> str | None:
    """Return the AUTHOR affiliation name from a searchHit, or None."""
    for aff in item.get("affiliations", []):
        if aff.get("afType", "").upper() == "AUTHOR":
            return aff.get("affiliation")
    return None



def _infer_doc_type(item: dict) -> str:
    """Infer FERC doc_type from classTypes[], falling back to description keywords."""
    for ct in item.get("classTypes", []):
        doc_type_str = ct.get("documentType", "").lower()
        for key, val in _DOC_TYPE_MAP.items():
            if key in doc_type_str:
                return val
    desc = item.get("description", "").lower()
    for key, val in _DOC_TYPE_MAP.items():
        if key in desc:
            return val
    return "ferc-filing"
