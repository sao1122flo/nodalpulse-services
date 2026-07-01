"""Lightweight existence probes for on-demand crawl validation.

Each probe makes a single HTTP round-trip to confirm the proceeding/docket
exists in the source system.  Returns the reported total doc count (≥1 means
valid), or 0 on not-found, invalid format, or any I/O error (fail-open so a
transient network hiccup does not leave a ghost docket).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import httpx

from nodalpulse.crawlers.cpuc import (
    _HEADERS as _CPUC_HEADERS,
)
from nodalpulse.crawlers.cpuc import (
    _NUM_RESULTS_RE,
    _PROC_VALID_RE,
    _init_session,
    _post_search,
    normalize_proc,
)
from nodalpulse.crawlers.ferc import (
    _HEADERS as _FERC_HEADERS,
)
from nodalpulse.crawlers.ferc import (
    _SEARCH_URL as _FERC_SEARCH_URL,
)
from nodalpulse.crawlers.ferc import (
    _normalize_docket,
)
from nodalpulse.crawlers.puct import (
    FILINGS_URL as _PUCT_FILINGS_URL,
)
from nodalpulse.crawlers.puct import (
    _parse_filing_results as _parse_puct_filings,
)

logger = logging.getLogger(__name__)

# How far back to search when probing — wide enough to catch dormant proceedings
_PROBE_LOOKBACK_DAYS = 365


async def probe_puct(control_number: str) -> int:
    """Fetch one PUCT L2 filings page for *control_number* (all-time) and return the
    filing-item count. Single round-trip, verify=False (Interchange's cert chain is
    broken — the crawler uses verify=False too). Returns 0 on invalid format, 0 items,
    or any network/parse error (fail-open). This is the coverage-gap fix's validation:
    a control number with items on Interchange but 0 in our DB was never crawled."""
    cn = (control_number or "").strip()
    if not cn.isdigit():
        logger.info("probe_puct: %r → invalid (non-digit control number)", control_number)
        return 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            verify=False,
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            r = await client.get(
                _PUCT_FILINGS_URL,
                params={
                    "ControlNumber": cn,
                    "DateFiledFrom": "2000-01-01",
                    "DateFiledTo": date.today().isoformat(),
                    "ItemMatch": "0",
                },
            )
            r.raise_for_status()
            total = len(_parse_puct_filings(r.text, cn))
            logger.info("probe_puct: control_number=%s → %d filing items", cn, total)
            return total
    except Exception:
        logger.exception("probe_puct: unexpected error probing control_number=%s", control_number)
        return 0


async def probe_cpuc(proc: str) -> int:
    """POST one CPUC search for *proc* and return the reported numResults.

    Uses the same session-init + form-POST flow as CpucAdapter, but stops after
    the first HTTP exchange (no pagination).  Returns 0 on invalid format, 0 docs,
    or any network / parse error.
    """
    normalized = normalize_proc(proc)
    if not _PROC_VALID_RE.match(normalized):
        logger.info("probe_cpuc: %r → invalid format after normalizing to %r", proc, normalized)
        return 0

    since = date.today() - timedelta(days=_PROBE_LOOKBACK_DAYS)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers=_CPUC_HEADERS,
        ) as client:
            session = await _init_session(client)
            if not session:
                logger.warning("probe_cpuc: session init failed for proc=%s", normalized)
                return 0
            r = await _post_search(client, session, normalized, since)
            m = _NUM_RESULTS_RE.search(r.text)
            total = int(m.group(1)) if m else 0
            logger.info("probe_cpuc: proc=%s → %d docs (since=%s)", normalized, total, since)
            return total
    except Exception:
        logger.exception("probe_cpuc: unexpected error probing proc=%s", proc)
        return 0


async def probe_ferc(docket: str) -> int:
    """POST one FERC AdvancedSearch for *docket* and return totalHits.

    Uses resultsPerPage=1 to minimise payload.  Returns 0 on invalid docket,
    0 results, or any network / parse error.
    """
    normalized = _normalize_docket(docket)
    if not normalized:
        return 0

    body = {
        "searchText": "*",
        "searchFullText": True,
        "searchDescription": True,
        "docketSearches": [{"docketNumber": normalized, "subDocketNumbers": []}],
        "dateSearches": [],
        "affiliations": [],
        "categories": [],
        "libraries": ["Electric"],
        "classTypes": [],
        "allDates": True,
        "resultsPerPage": 1,
        "curPage": 1,
        "sortBy": "",
        "groupBy": "NONE",
        "eFiling": False,
        "accessionNumber": None,
    }

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers=_FERC_HEADERS,
        ) as client:
            resp = await client.post(_FERC_SEARCH_URL, content=json.dumps(body))
            resp.raise_for_status()
            data = resp.json()
            total = data.get("totalHits") or 0
            logger.info("probe_ferc: docket=%s → %d docs", normalized, total)
            return total
    except Exception:
        logger.exception("probe_ferc: unexpected error probing docket=%s", docket)
        return 0
