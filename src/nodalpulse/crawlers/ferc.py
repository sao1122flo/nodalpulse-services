"""FERC eLibrarywebapi adapter — shared FERC layer for CAISO + PJM.

Polls https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch
per watched docket and emits normalized RawFiling objects.

Replaces the defunct ecollection.ferc.gov/api/rssfeed (returned XBRL data,
not tariff filings — zero useful hits since the adapter was built).

PDF strategy by filer:
- PJM own filings (AUTHOR affiliation == "PJM Interconnection, L.L.C.") →
  source_url = pjm.com/-/media/DotCom/documents/ferc/filings/{year}/{YYYYMMDD}-{docket}-000.pdf
- All others → source_url = ""; metadata["ferc_file_id"] carries transmittals[0].fileId
  for DownloadFile+session fetch at extraction time (gating issue tracked in #64).

Design decisions:
- Multi-docket: docketNumbers[] list → all captured in metadata["docket_numbers"]
- Dedup key: acesssionNumber (FERC API typo, field name preserved) → external_id, YYYYMMDD-NNNN
- Filed cursor: filedDate (MM/DD/YYYY) — not postedDate or issuedDate
- Sequential per-docket queries; no parallel calls to FERC servers
- Discovery pump removed — broken affiliations filter (service-list match, not author);
  new PJM dockets are seeded manually, consistent with all other jurisdictions
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta

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

# PJM Interconnection filer string as it appears in the FERC API affiliations[]
_PJM_FILER = "PJM Interconnection, L.L.C."

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
        until_date = date.today()

        if not self._watched:
            logger.info("FercAdapter: watch set empty — no dockets to poll")
            return []

        logger.info(
            "FercAdapter: polling %d dockets since=%s until=%s",
            len(self._watched), since_date, until_date,
        )

        filings: list[RawFiling] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:
            for docket in sorted(self._watched):
                items = await _fetch_docket(client, docket, since_date, until_date)
                for item in items:
                    filing = _item_to_filing(item, since_date)
                    if filing and filing.external_id not in seen_ids:
                        filings.append(filing)
                        seen_ids.add(filing.external_id)

        logger.info("FercAdapter: %d new filings across %d dockets", len(filings), len(self._watched))
        return filings


# ── eLibrarywebapi fetch ──────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_docket(
    client: httpx.AsyncClient,
    docket: str,
    since: date,
    until: date,
) -> list[dict]:
    """Fetch all filings for one docket in [since, until] via AdvancedSearch, paginated."""
    items: list[dict] = []
    page = 1

    while True:
        body = {
            "searchText": "*",
            "searchFullText": True,
            "searchDescription": True,
            "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
            "dateSearches": [
                {
                    "startDate": since.strftime("%m-%d-%Y"),
                    "endDate": until.strftime("%m-%d-%Y"),
                    "dateType": "Filed Date",
                }
            ],
            "affiliations": [],
            "categories": [],
            "libraries": [],
            "classTypes": [],
            "accessionNumber": None,
            "eFiling": False,
            "resultsPerPage": _RESULTS_PER_PAGE,
            "curPage": page,
            "groupBy": "NONE",
            "sortBy": "",
            "allDates": False,
        }

        logger.debug("FercAdapter: AdvancedSearch docket=%s since=%s page=%d", docket, since, page)
        resp = await client.post(_SEARCH_URL, content=json.dumps(body))
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("searchHits", [])
        items.extend(batch)
        total = data.get("totalHits", 0)

        logger.info(
            "FercAdapter: docket=%s page=%d got=%d total=%d",
            docket, page, len(batch), total,
        )

        if len(batch) < _RESULTS_PER_PAGE or len(items) >= total:
            break
        page += 1

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

    docket_numbers = [_normalize_docket(d) for d in item.get("docketNumbers", [])]
    description = item.get("description", "")
    filer = _get_author(item)
    is_pjm = filer == _PJM_FILER

    # PDF source URL: PJM.com slug for PJM filings; empty for FERC orders (deferred)
    source_url = _pjm_pdf_url(item, docket_numbers) if is_pjm else ""

    # For non-PJM: store first transmittal's fileId for extraction-time DownloadFile+session
    transmittals = item.get("transmittals", [])
    ferc_file_id = transmittals[0].get("fileId", "") if transmittals else ""

    return RawFiling(
        source_slug="ferc",
        external_id=acc,
        doc_type=_infer_doc_type(item),
        title=description,
        source_url=source_url,
        filed_at=filed.isoformat() + "T00:00:00+00:00",
        content=b"",   # deferred — R2 upload happens at extraction time
        file_ext="pdf",
        metadata={
            "docket_numbers": docket_numbers,
            "raw_title": description,
            "description": description,
            "filer": filer,
            "ferc_file_id": ferc_file_id,
            "ferc_accession": acc,
            "is_pjm_filing": is_pjm,
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


def _pjm_pdf_url(item: dict, docket_numbers: list[str]) -> str:
    """Construct the PJM.com PDF URL for a PJM-authored FERC filing.

    Pattern: pjm.com/-/media/DotCom/documents/ferc/filings/{year}/{YYYYMMDD}-{docket}-000.pdf
    where YYYYMMDD is the filed date and {docket} is the primary captioned docket.
    """
    filed = _parse_filed_date(item.get("filedDate", ""))
    if not filed or not docket_numbers:
        return ""
    primary = docket_numbers[0]
    return (
        f"https://www.pjm.com/-/media/DotCom/documents/ferc/filings"
        f"/{filed.year}/{filed.strftime('%Y%m%d')}-{primary}-000.pdf"
    )


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
