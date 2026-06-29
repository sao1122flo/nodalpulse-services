"""IMM (Independent Market Monitor / Monitoring Analytics) adapter.

Crawls two Monitoring Analytics indexes (both curated SSI .shtml pages —
directory browsing at /filings/{year}/ is OFF, so that path returns an empty
body; re-pointed 2026-06-29):
  - /filings/{year}.shtml — FERC filings (docket + YYYYMMDD in the filename)
  - /reports/PJM_State_of_the_Market/{year}.shtml — annual/quarterly State of the
    Market reports (no date in filename → derived from the year/quarter token)

Filename patterns (as of 2026-06):
  filings:  IMM_Answer_Docket_No_EC26-76_20260619.pdf
            IMM_Complaint_re_Data_Center_Loads_Docket_No_EL26-119_20251125.pdf
  reports:  2026q1-som-pjm.pdf  ·  2025-som-pjm-vol1.pdf  (section/toc/preface skipped)

Docket linkage: FERC docket IDs parsed from the filename are written to the
dockets table as PJM-FERC via run_adapter → find_or_create_docket. These dockets
then enter the FercAdapter watch set via get_pjm_ferc_docket_set() automatically
(option (c) from engineering-kickoff-brief.md §T9 discovery discussion).

Provisional dockets (ending in -XX, -YY, etc.) are excluded from docket_numbers
since they carry no stable FERC ID yet; they will be resolved in a future pass.

R2 strategy: content=b"" — source_url stored at crawl time; PDF uploaded to R2
at extraction time (same deferred pattern as FercAdapter).

Discovery floor: 2025-01-01 — avoids bootstrapping the full pre-2025 back-catalog
on first run. Adjust WORKER_IMM_SINCE_FLOOR env var to change.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from datetime import UTC, date, datetime
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_ROOT = "https://www.monitoringanalytics.com"
# The filings INDEX is a curated SSI page at /filings/{year}.shtml — NOT the
# /filings/{year}/ directory (directory browsing is off → that path returns an
# empty body, which silently produced {saved:0} daily). The PDFs themselves
# live at /filings/{year}/<file>.pdf and carry docket + YYYYMMDD in the name.
_BASE_URL = f"{_ROOT}/filings"
# State of the Market reports live under a SEPARATE path (not /filings/). Their
# filenames carry no date — the period is derived from the year/quarter token.
_SOM_BASE = f"{_ROOT}/reports/PJM_State_of_the_Market"

# ISO date encoded at end of filename: ..._20231107.pdf
_DATE_RE = re.compile(r"_(\d{8})(?:\.pdf)?$", re.IGNORECASE)

# SOM report filenames: 2026q1-som-pjm.pdf (quarterly full) or 2025-som-pjm-vol1.pdf
# (annual volume). Section/aux files (-sec, -toc, -preface, …) are skipped — we
# ingest the consolidated report(s) per period, not all ~15 section PDFs.
_SOM_MAIN_RE = re.compile(r"(\d{4})(?:q([1-4]))?-som-pjm(?:-vol(\d+))?\.pdf$", re.IGNORECASE)
_SOM_SKIP_RE = re.compile(r"-(sec\d*|toc|preface|appendix|intro|errata)\.pdf$", re.IGNORECASE)
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}

# FERC docket ID in filename: Docket_No_EL24-12 or Docket_No_ER25-1357.
# Requires a digit-only sequence after the dash (\d+) — this intentionally
# excludes provisional IDs like EL26-XX to avoid polluting the dockets table
# with unresolvable placeholders.
#
# Known gap (R8): the marquee IMM data-center complaint was filed as EL26-XX
# (provisional) and is NOT tracked by docket ID until FERC assigns a real
# number. Auto-heal: when IMM renames the file with the real docket (e.g.
# EL26-119), the next crawl picks it up; get_pjm_ferc_docket_set() adds it to
# the FercAdapter watch set the same day. Document in /limitations:
# "IMM filings with provisional docket IDs (e.g. EL26-XX) are ingested as
#  documents but not linked by docket until FERC assigns a permanent number."
_DOCKET_FROM_FILENAME_RE = re.compile(
    r"Docket[_\s-]?No[_\s.]?([A-Z]{1,4}\d{2}-\d+)",
    re.IGNORECASE,
)

# First-keyword-after-IMM_ → canonical doc_type (longest match wins)
_TYPE_MAP: list[tuple[str, str]] = [
    ("state_of_the_market", "imm-state-of-market"),
    ("state_of_market", "imm-state-of-market"),
    ("annual_state", "imm-state-of-market"),
    ("complaint", "imm-complaint"),
    ("answer", "imm-answer"),
    ("reply_brief", "imm-brief"),
    ("initial_brief", "imm-brief"),
    ("reply", "imm-reply"),
    ("brief", "imm-brief"),
    ("motion", "imm-motion"),
    ("petition", "imm-petition"),
    ("report", "imm-report"),
    ("comments", "imm-comment"),
    ("comment", "imm-comment"),
]

# Env-configurable floor so operators can back-fill without touching code.
_SINCE_FLOOR = date.fromisoformat(os.environ.get("WORKER_IMM_SINCE_FLOOR", "2025-01-01"))


def _parse_filename(filename: str) -> tuple[str, list[str], date | None]:
    """Parse IMM PDF filename → (doc_type, docket_numbers, filed_date).

    docket_numbers is empty [] when no valid FERC docket is found (e.g. State
    of the Market reports, provisional XX dockets). The filing is still ingested
    so the PDF is available for extraction — it just won't link to a docket row.
    """
    stem = filename
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]

    lower = stem.lower()

    # Doc type — longest match first (list is ordered longest-first)
    doc_type = "imm-filing"
    for keyword, mapped in _TYPE_MAP:
        if keyword in lower:
            doc_type = mapped
            break

    # Filed date from trailing _YYYYMMDD
    filed_date: date | None = None
    dm = _DATE_RE.search(stem)
    if dm:
        raw = dm.group(1)
        with contextlib.suppress(ValueError):
            filed_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))

    # Docket IDs — may be multi-captioned in rare cases; take all matches
    dockets: list[str] = []
    for m in _DOCKET_FROM_FILENAME_RE.finditer(stem):
        raw_docket = m.group(1).strip().upper()
        dockets.append(raw_docket)

    return doc_type, dockets, filed_date


def _parse_som_filename(filename: str) -> tuple[date | None, str]:
    """State-of-the-Market report filename → (period_end_date, human label).

    Returns (None, "") for section/aux PDFs and anything that isn't a main report.
    The period-end date is derived from the year/quarter token (annual → Dec 31).
    """
    if _SOM_SKIP_RE.search(filename):
        return None, ""
    m = _SOM_MAIN_RE.search(filename)
    if not m:
        return None, ""
    year = int(m.group(1))
    quarter = m.group(2)
    vol = m.group(3)
    if quarter:
        mo, day = _QUARTER_END[int(quarter)]
        label = f"{year} Q{quarter} State of the Market Report for PJM"
    else:
        mo, day = 12, 31
        label = f"{year} Annual State of the Market Report for PJM"
        if vol:
            label += f" (Vol {vol})"
    try:
        return date(year, mo, day), label
    except ValueError:
        return None, label


def _make_title(filename: str, dockets: list[str], filed_date: date | None) -> str:
    """Construct a human-readable title from the filename parts."""
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    # Replace underscores (but keep hyphens in docket IDs)
    readable = stem.replace("_", " ")
    # Shorten to 200 chars for the DB title column
    return readable[:200]


class ImmAdapter(MarketAdapter):
    """Crawls the IMM (Monitoring Analytics) filings site for FERC docket filings.

    Scans static HTML year-pages, extracts PDF links, parses metadata from
    filenames. High-signal, low-volume: typically <20 filings/year.
    """

    source_slug = "imm"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = date.fromisoformat(since) if since else date.today()
        # Apply floor so first run doesn't bootstrap the full back-catalog.
        effective_since = max(since_date, _SINCE_FLOOR)

        years = range(effective_since.year, date.today().year + 1)
        filings: list[RawFiling] = []

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            for year in years:
                filings.extend(await self._fetch_year(client, year, effective_since))
                filings.extend(await self._fetch_som_year(client, year, effective_since))

        logger.info(
            "ImmAdapter: since=%s returning %d filings across %d year(s)",
            effective_since,
            len(filings),
            len(list(years)),
        )
        return filings

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _fetch_year(
        self, client: httpx.AsyncClient, year: int, since_date: date
    ) -> list[RawFiling]:
        url = f"{_BASE_URL}/{year}.shtml"
        logger.debug("ImmAdapter: fetching %s", url)
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.debug("ImmAdapter: year %d not found (404)", year)
                return []
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise

        if not resp.text.strip():
            logger.warning(
                "ImmAdapter: %s returned an empty body — the IMM filings index structure "
                "may have changed (silent-zero failure mode)",
                url,
            )
            return []
        return self._parse_page(resp.text, year, since_date, url)

    def _parse_page(self, html: str, year: int, since_date: date, page_url: str) -> list[RawFiling]:
        tree = HTMLParser(html)
        filings: list[RawFiling] = []

        for a in tree.css("a[href]"):
            href = a.attributes.get("href", "")
            if not href.lower().endswith(".pdf"):
                continue

            # href may be a bare filename or a relative/absolute path
            filename = href.split("/")[-1]
            if not filename:
                continue

            doc_type, dockets, filed_date = _parse_filename(filename)

            # Date filter — skip if we can't determine date or it's too old
            if filed_date is None or filed_date < since_date:
                continue

            source_url = urljoin(page_url, href)
            title = _make_title(filename, dockets, filed_date)
            external_id = filename[:-4] if filename.lower().endswith(".pdf") else filename

            filings.append(
                RawFiling(
                    source_slug="imm",
                    external_id=external_id,
                    doc_type=doc_type,
                    title=title,
                    source_url=source_url,
                    filed_at=datetime.combine(filed_date, datetime.min.time())
                    .replace(tzinfo=UTC)
                    .isoformat(),
                    content=b"",  # deferred R2 — uploaded at extraction time
                    file_ext="pdf",
                    metadata={
                        "docket_numbers": dockets,
                        "raw_filename": filename,
                        "year": year,
                    },
                )
            )

        logger.info(
            "ImmAdapter: year=%d parsed %d filings (since=%s)", year, len(filings), since_date
        )
        return filings

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _fetch_som_year(
        self, client: httpx.AsyncClient, year: int, since_date: date
    ) -> list[RawFiling]:
        """Fetch the State of the Market index for a year and ingest the main reports.

        These are the marquee IMM publications (annual + quarterly), which live under
        /reports/PJM_State_of_the_Market/{year}.shtml — a different path from /filings/.
        """
        url = f"{_SOM_BASE}/{year}.shtml"
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise

        if not resp.text.strip():
            logger.warning(
                "ImmAdapter: SOM %s returned an empty body — structure may have changed", url
            )
            return []
        return self._parse_som_page(resp.text, year, since_date, url)

    def _parse_som_page(
        self, html: str, year: int, since_date: date, page_url: str
    ) -> list[RawFiling]:
        tree = HTMLParser(html)
        filings: list[RawFiling] = []
        seen: set[str] = set()

        for a in tree.css("a[href]"):
            href = a.attributes.get("href", "")
            if not href.lower().endswith(".pdf"):
                continue
            filename = href.split("/")[-1]
            if not filename:
                continue

            period_end, label = _parse_som_filename(filename)
            if period_end is None or period_end < since_date:
                continue

            external_id = filename[:-4] if filename.lower().endswith(".pdf") else filename
            if external_id in seen:
                continue
            seen.add(external_id)

            filings.append(
                RawFiling(
                    source_slug="imm",
                    external_id=external_id,
                    doc_type="imm-state-of-market",
                    title=label or _make_title(filename, [], period_end),
                    source_url=urljoin(page_url, href),
                    filed_at=datetime.combine(period_end, datetime.min.time())
                    .replace(tzinfo=UTC)
                    .isoformat(),
                    content=b"",  # deferred R2 — uploaded at extraction time
                    file_ext="pdf",
                    metadata={
                        "docket_numbers": [],
                        "raw_filename": filename,
                        "year": year,
                        "report": True,
                    },
                )
            )

        logger.info(
            "ImmAdapter: SOM year=%d parsed %d report(s) (since=%s)", year, len(filings), since_date
        )
        return filings
