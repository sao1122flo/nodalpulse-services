"""FERC broad-sweep discovery fetcher (issue #85).

Polls AdvancedSearch WITHOUT a docket constraint, returning recent Electric-library
filings as lightweight DiscoveryItem objects for entity-match.

Key differences from FercAdapter (ferc.py):
- docketSearches=[] — no docket constraint; sweeps all Electric-library filings
- Returns DiscoveryItem, not RawFiling — no R2, no PDF, no extraction, no LLM
- AUTHOR-type affiliations only stored in filer_names; the full affiliations[]
  array includes service-list entries (can be thousands per docket) which would
  make entity matching useless. AUTHOR = the actual filer / applicant.
- Docket prefix allowlist applied in Python (ER/EL/RM/RD/RT/EI/QF) to skip
  gas/pipeline/hydro dockets that slip through the Electric library filter.
- Governor: max_filings cap applied before returning (see DISCOVERY_MAX_FILINGS env).

Prior attempt note: the original discovery pump (removed in ferc.py) tried to use
the affiliations array as a docket-discovery signal — which failed because service
lists pollute that array. This module is different: we're building a metadata store
for entity-name matching, not docket discovery.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_BASE = "https://elibrary.ferc.gov/eLibrarywebapi/api"
_SEARCH_URL = f"{_BASE}/Search/AdvancedSearch"
_RESULTS_PER_PAGE = 50
_MAX_PAGES = 20  # hard cap: 1000 API results per sweep; daily incremental is ~2-4 pages

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 NodalPulse/1.0 regulatory-monitor",
    "Origin": "https://elibrary.ferc.gov",
    "Referer": "https://elibrary.ferc.gov/",
}

# FERC electric docket prefixes to keep. Excludes gas (CP/RP/NOR), hydro (P/PF/EW),
# oil pipeline (OR), and administrative (AD/RM policy-only) noise.
_ALLOWED_PREFIXES = frozenset({"ER", "EL", "RM", "RD", "RT", "EI", "QF"})

_SUB_DOCKET_RE = re.compile(r"-0\d{2}$")

# Reuse same doc-type inference as FercAdapter to stay consistent.
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


@dataclass
class DiscoveryItem:
    accession: str
    jurisdiction: str
    description: str
    filer_names: list[str]
    docket_numbers: list[str]
    filed_at: str    # YYYY-MM-DD
    doc_type: str


async def fetch_ferc_discovery(
    since_date: date,
    max_filings: int = 500,
    max_pages: int = _MAX_PAGES,
) -> list[DiscoveryItem]:
    """Broad-sweep all FERC Electric-library filings since since_date.

    Returns at most max_filings items, sorted most-recent-first (API order preserved).
    Empty list if the API is unreachable after retries.

    max_pages: override page cap for deep backfills (daily sweeps need 2-4 pages;
    a backfill covering 2+ months needs 60-100 pages at ~63 filings/day).
    """
    logger.info(
        "fetch_ferc_discovery: broad sweep since=%s max=%d max_pages=%d",
        since_date, max_filings, max_pages,
    )

    items: list[DiscoveryItem] = []
    seen_accessions: set[str] = set()

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_HEADERS) as client:
        page = 1
        while page <= max_pages and len(items) < max_filings:
            body = {
                "searchText": "*",
                "searchFullText": False,    # metadata only — no full document text
                "searchDescription": False, # no text filter; return all Electric filings
                "docketSearches": [],       # no docket constraint — broad sweep
                "dateSearches": [],
                "affiliations": [],
                "categories": [],
                "libraries": ["Electric"],
                "classTypes": [],
                "accessionNumber": None,
                "eFiling": False,
                "resultsPerPage": _RESULTS_PER_PAGE,
                "curPage": page,
                "groupBy": "NONE",
                "sortBy": "",   # default = filedDate DESC
                "allDates": True,
            }

            try:
                data = await _post_with_retry(client, body)
            except Exception:
                logger.exception("fetch_ferc_discovery: page=%d failed, stopping", page)
                break

            batch = data.get("searchHits") or []
            total = data.get("totalHits") or 0
            logger.info(
                "fetch_ferc_discovery: page=%d got=%d total=%d items_so_far=%d",
                page, len(batch), total, len(items),
            )

            if not batch:
                break

            for raw in batch:
                item = _item_to_discovery(raw, since_date)
                if item is not None and item.accession not in seen_accessions:
                    items.append(item)
                    seen_accessions.add(item.accession)
                if len(items) >= max_filings:
                    break

            # Early-stop based on page-tail date (same pattern as FercAdapter._fetch_docket).
            # Separate from prefix filtering: a page where all items pass the date but fail
            # the prefix allowlist should NOT trigger early-stop — keep paginating.
            last_filed = _parse_filed_date(batch[-1].get("filedDate", "")) if batch else None
            if last_filed and last_filed < since_date:
                logger.info(
                    "fetch_ferc_discovery: early-stop at page=%d (tail=%s < since=%s)",
                    page, last_filed, since_date,
                )
                break

            if len(batch) < _RESULTS_PER_PAGE or len(items) >= max_filings:
                break

            page += 1

    if page > max_pages:
        logger.warning("fetch_ferc_discovery: hit page cap (%d)", max_pages)

    logger.info("fetch_ferc_discovery: returning %d items", len(items))
    return items


# ── internals ──────────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _post_with_retry(client: httpx.AsyncClient, body: dict) -> dict:
    resp = await client.post(_SEARCH_URL, content=json.dumps(body))
    resp.raise_for_status()
    return resp.json()


def _item_to_discovery(raw: dict, since_date: date) -> DiscoveryItem | None:
    """Convert one searchHit to DiscoveryItem, or None if filtered out."""
    accession = raw.get("acesssionNumber", "")
    if not accession:
        return None

    filed = _parse_filed_date(raw.get("filedDate", ""))
    if not filed or filed < since_date:
        return None

    docket_numbers = _normalize_dockets(raw.get("docketNumbers", []))

    # Prefix allowlist: skip gas/hydro/pipeline filings.
    # Use first docket number as the primary signal.
    if docket_numbers:
        prefix = re.match(r"^([A-Z]+)", docket_numbers[0])
        if not prefix or prefix.group(1) not in _ALLOWED_PREFIXES:
            return None

    description = (raw.get("description") or "").strip()
    if not description:
        return None

    # AUTHOR-type only. Full affiliations[] includes service-list entries
    # (thousands per large docket) — matching against those produces noise identical
    # to full-text search. AUTHOR = filer / applicant = the party who submitted.
    filer_names = [
        aff["affiliation"]
        for aff in raw.get("affiliations", [])
        if aff.get("afType", "").upper() == "AUTHOR"
        and aff.get("affiliation")
    ]

    return DiscoveryItem(
        accession=accession,
        jurisdiction="FERC",
        description=description,
        filer_names=filer_names,
        docket_numbers=docket_numbers,
        filed_at=filed.isoformat(),
        doc_type=_infer_doc_type(raw),
    )


def _normalize_dockets(raw_list: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for raw in raw_list:
        nd = _SUB_DOCKET_RE.sub("", raw.strip().upper())
        if nd not in seen:
            seen[nd] = None
    return list(seen.keys())


def _parse_filed_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def _infer_doc_type(raw: dict) -> str:
    for ct in raw.get("classTypes", []):
        s = ct.get("documentType", "").lower()
        for key, val in _DOC_TYPE_MAP.items():
            if key in s:
                return val
    desc = (raw.get("description") or "").lower()
    for key, val in _DOC_TYPE_MAP.items():
        if key in desc:
            return val
    return "ferc-filing"
