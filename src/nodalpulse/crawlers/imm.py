"""IMM (Independent Market Monitor / Monitoring Analytics) adapter.

Crawls https://www.monitoringanalytics.com/filings/{year}/ — a static HTML
directory listing of PDFs with predictable, information-dense filenames.

Filename patterns (as of 2026-06):
  IMM_Complaint_Docket_No_EL24-12_20231107.pdf
  IMM_Answer_Docket_No_EL25-49_20250301.pdf
  IMM_Complaint_re_Data_Center_Loads_Docket_No_EL26-119_20251125.pdf
  IMM_State_of_the_Market_Report_for_PJM_2025_Q1.pdf  (no docket → skipped)

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

import logging
import os
import re
from datetime import date, datetime, timezone

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.monitoringanalytics.com/filings"

# ISO date encoded at end of filename: ..._20231107.pdf
_DATE_RE = re.compile(r"_(\d{8})(?:\.pdf)?$", re.IGNORECASE)

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
    ("state_of_the_market",  "imm-state-of-market"),
    ("state_of_market",      "imm-state-of-market"),
    ("annual_state",         "imm-state-of-market"),
    ("complaint",            "imm-complaint"),
    ("answer",               "imm-answer"),
    ("reply_brief",          "imm-brief"),
    ("initial_brief",        "imm-brief"),
    ("reply",                "imm-reply"),
    ("brief",                "imm-brief"),
    ("motion",               "imm-motion"),
    ("petition",             "imm-petition"),
    ("report",               "imm-report"),
    ("comments",             "imm-comment"),
    ("comment",              "imm-comment"),
]

# Env-configurable floor so operators can back-fill without touching code.
_SINCE_FLOOR = date.fromisoformat(
    os.environ.get("WORKER_IMM_SINCE_FLOOR", "2025-01-01")
)


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
        try:
            filed_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            pass

    # Docket IDs — may be multi-captioned in rare cases; take all matches
    dockets: list[str] = []
    for m in _DOCKET_FROM_FILENAME_RE.finditer(stem):
        raw_docket = m.group(1).strip().upper()
        dockets.append(raw_docket)

    return doc_type, dockets, filed_date


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
                year_filings = await self._fetch_year(client, year, effective_since)
                filings.extend(year_filings)

        logger.info(
            "ImmAdapter: since=%s returning %d filings across %d year(s)",
            effective_since, len(filings), len(list(years)),
        )
        return filings

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _fetch_year(
        self, client: httpx.AsyncClient, year: int, since_date: date
    ) -> list[RawFiling]:
        url = f"{_BASE_URL}/{year}/"
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

        return self._parse_page(resp.text, year, since_date)

    def _parse_page(self, html: str, year: int, since_date: date) -> list[RawFiling]:
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

            source_url = (
                href if href.startswith("http")
                else f"{_BASE_URL}/{year}/{filename}"
            )
            title = _make_title(filename, dockets, filed_date)
            external_id = filename[:-4] if filename.lower().endswith(".pdf") else filename

            filings.append(RawFiling(
                source_slug="imm",
                external_id=external_id,
                doc_type=doc_type,
                title=title,
                source_url=source_url,
                filed_at=datetime.combine(filed_date, datetime.min.time()).replace(
                    tzinfo=timezone.utc
                ).isoformat(),
                content=b"",  # deferred R2 — uploaded at extraction time
                file_ext="pdf",
                metadata={
                    "docket_numbers": dockets,
                    "raw_filename": filename,
                    "year": year,
                },
            ))

        logger.info("ImmAdapter: year=%d parsed %d filings (since=%s)", year, len(filings), since_date)
        return filings
