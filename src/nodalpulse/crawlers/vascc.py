"""VA SCC (Virginia State Corporation Commission) DocketSearch adapter.

Source: https://www.scc.virginia.gov/docketsearch — a Durandal/Knockout SPA whose
docket data comes from a Breeze.js WebAPI. The SPA shell geo-restricts (CloudFront,
US-only), but the JSON API behind it is a plain GET from a US egress. We hit the same
"Daily Filings" feed the site's dailyFilings view uses:

    GET {API}/breeze/DailyFilings/GetAllDailyFilings
        ?$filter=Year eq <y> and Month eq <m>&$orderby=Day

It returns one row per filed document: CaseNumber, CaseName, DocName, Year/Month/Day,
DateFiled, DocID, FileName. The feed is ALL-industry (electric + gas + water +
securities + insurance + pipeline-safety + underground-damage), so VA electric scope
must filter.

Scope (case-scoped, like MdPscAdapter): CaseName is the *case's regulated utility* on
EVERY row — intervenor, staff, and Commission filings all carry the utility's CaseName
(the leading name in DocName is the filer, NOT the case). So keying electric scope off
CaseName captures orders and intervenor filings that a filer-name filter would drop.
A row is electric if its CaseName names an electric utility/cooperative OR its
CaseNumber is in the persistent watch set (dockets jurisdiction='VA-SCC').

Row shape → RawFiling: external_id = DocID (stable per-document int); source_url =
{DOCS}/{encodeURIComponent(FileName)} (verified 200 application/pdf); deferred R2.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_API_BASE = "https://www.scc.virginia.gov/DocketSearchAPI/breeze/DailyFilings"
_RESOURCE = "GetAllDailyFilings"
# PROD doc store (services/model.js PDFurl). https works and CloudFront-fronted.
_DOCS_BASE = "https://www.scc.virginia.gov/docketsearch/DOCS/"
_ET = ZoneInfo("America/New_York")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 NodalPulse/1.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.scc.virginia.gov/docketsearch",
}

# Electric utilities/cooperatives regulated by VA SCC — lowercased substrings matched
# against CaseName. "electric" catches Dominion ("Virginia Electric and Power") and
# every "... Electric Cooperative"; gas/water/telecom/securities CaseNames never
# contain "electric". The rest are electric utilities whose names lack the word.
_ELECTRIC_NAME = (
    "electric",  # Virginia Electric and Power (Dominion); all "* Electric Coop[erative]"
    "appalachian power",  # APCo (AEP) — SW Virginia, PJM/AEP zone
    "old dominion power",  # Kentucky Utilities d/b/a Old Dominion Power
    "kentucky utilities",  # same entity, alternate CaseName
)

_DOC_TYPE_MAP: list[tuple[str, str]] = [
    ("final order", "vascc-order"),
    ("order", "vascc-order"),
    ("application", "vascc-application"),
    ("petition", "vascc-petition"),
    ("complaint", "vascc-complaint"),
    ("tariff", "vascc-tariff"),
    ("direct testimony", "vascc-testimony"),
    ("rebuttal testimony", "vascc-testimony"),
    ("testimony", "vascc-testimony"),
    ("brief", "vascc-brief"),
    ("comment", "vascc-comments"),
    ("settlement", "vascc-settlement"),
    ("stipulation", "vascc-settlement"),
    ("motion", "vascc-motion"),
    ("notice", "vascc-notice"),
    ("report", "vascc-report"),
    ("hearing", "vascc-hearing"),
    ("transcript", "vascc-hearing"),
    ("rule", "vascc-rulemaking"),
    ("regulation", "vascc-rulemaking"),
    ("letter", "vascc-correspondence"),
    ("memorandum", "vascc-correspondence"),
    ("cover letter", "vascc-correspondence"),
]


# ── parsing helpers (pure → hermetically testable) ────────────────────────────────


def is_electric_case(case_name: str) -> bool:
    n = (case_name or "").lower()
    return any(k in n for k in _ELECTRIC_NAME)


def _doc_type(doc_name: str) -> str:
    d = (doc_name or "").lower()
    for key, val in _DOC_TYPE_MAP:
        if key in d:
            return val
    return "vascc-filing"


def _encode_filename(file_name: str) -> str:
    """Match the SPA's encodeURIComponent(FileName): escape everything except the
    JS-unreserved set. FileNames look like "8d4@01!.PDF" → "8d4%4001!.PDF"."""
    return quote(file_name, safe="!*'()-_.~")


def _filed_at(year: int, month: int, day: int) -> str:
    """VA filing date (Eastern, date-only) → UTC ISO-8601 (midnight ET)."""
    return datetime(year, month, day, tzinfo=_ET).astimezone(UTC).isoformat()


def parse_rows(body: str) -> list[dict]:
    """Parse a GetAllDailyFilings JSON response → row dicts. Pure: no network, no
    filtering. Handles both the bare-array form and the $inlinecount-wrapped
    {"Results": [...]} form, and tolerates a leading BOM."""
    body = body.lstrip("﻿").strip()
    if not body:
        return []
    data = json.loads(body)
    records = data.get("Results", []) if isinstance(data, dict) else data
    rows: list[dict] = []
    for rec in records:
        doc_id = rec.get("DocID")
        y, m, d = rec.get("Year"), rec.get("Month"), rec.get("Day")
        file_name = rec.get("FileName") or ""
        if doc_id is None or not (y and m and d) or not file_name:
            continue
        rows.append(
            {
                "doc_id": str(doc_id),
                "case_number": (rec.get("CaseNumber") or "").strip(),
                "case_name": (rec.get("CaseName") or "").strip(),
                "doc_name": (rec.get("DocName") or "").strip(),
                "file_name": file_name,
                "year": int(y),
                "month": int(m),
                "day": int(d),
                "filed_date": date(int(y), int(m), int(d)),
            }
        )
    return rows


def _month_windows(start: date, end: date) -> list[tuple[int, int]]:
    """Calendar (year, month) tuples covering [start, end] inclusive."""
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _to_filing(row: dict) -> RawFiling:
    return RawFiling(
        source_slug="vascc",
        external_id=row["doc_id"],
        doc_type=_doc_type(row["doc_name"]),
        title=row["doc_name"][:500],
        source_url=f"{_DOCS_BASE}{_encode_filename(row['file_name'])}",
        filed_at=_filed_at(row["year"], row["month"], row["day"]),
        content=b"",  # deferred R2 — extract worker fetches source_url post-triage
        file_ext="pdf",
        metadata={
            "docket_numbers": [row["case_number"]] if row["case_number"] else [],
            "case_name": row["case_name"],
            "doc_id": row["doc_id"],
            "posted_date": row["filed_date"].isoformat(),
        },
    )


# ── adapter ───────────────────────────────────────────────────────────────────────


class VaSccAdapter(MarketAdapter):
    """VA SCC DocketSearch adapter — case-scoped electric coverage over the
    all-industry Daily Filings Breeze feed.

    Args:
        watch_cases: VA-SCC case numbers already known to be electric (dockets table,
            jurisdiction='VA-SCC'), passed in by the handler. Unioned with the CaseName
            electric filter so a hand-added case (e.g. a developer-named transmission /
            data-center docket whose CaseName is not an electric utility) is captured
            in full across windows.
    """

    source_slug = "vascc"

    def __init__(self, watch_cases: set[str] | None = None) -> None:
        self._watch = {c.strip() for c in (watch_cases or set()) if c and c.strip()}

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )
        today = date.today()
        windows = _month_windows(since_date, today)
        logger.info(
            "VaSccAdapter: %s..%s (%d month windows, watch_set=%d)",
            since_date,
            today,
            len(windows),
            len(self._watch),
        )

        all_rows: list[dict] = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=90, headers=_HEADERS) as client:
            for y, m in windows:
                try:
                    rows = await self._fetch_month(client, y, m)
                    # month query returns the whole calendar month; clamp to [since, today]
                    all_rows.extend(r for r in rows if since_date <= r["filed_date"] <= today)
                except Exception:
                    logger.exception("VaSccAdapter: window %04d-%02d failed", y, m)

        return self._finalize(all_rows, since_date, today)

    async def _fetch_month(self, client: httpx.AsyncClient, year: int, month: int) -> list[dict]:
        """Fetch one calendar month of all-industry daily filings (no server cap)."""
        # No $orderby/$select: $select drops CaseName (needed for scoping) and an
        # unsupported $orderby field would fail the whole month → silent zero. We sort
        # in code (_finalize). The month query is uncapped (~hundreds of rows).
        url = f"{_API_BASE}/{_RESOURCE}?$filter=Year%20eq%20{year}%20and%20Month%20eq%20{month}"
        res = await client.get(url)
        res.raise_for_status()
        rows = parse_rows(res.text)
        logger.info("VaSccAdapter: %04d-%02d → %d raw rows", year, month, len(rows))
        return rows

    def _finalize(self, rows: list[dict], since_date: date, today: date) -> list[RawFiling]:
        seen: set[str] = set()
        keep: list[dict] = []
        for r in rows:
            if r["doc_id"] in seen:
                continue
            seen.add(r["doc_id"])
            if is_electric_case(r["case_name"]) or (
                r["case_number"] and r["case_number"] in self._watch
            ):
                keep.append(r)

        # Newest-first so run_adapter's max_filings cap (filings[:N]) keeps the most
        # recent — DocID is a monotonic per-document int, a stable same-day tiebreak.
        keep.sort(key=lambda r: (r["filed_date"], int(r["doc_id"])), reverse=True)
        filings = [_to_filing(r) for r in keep]

        # Anti-silent-zero floor: VA Daily Filings is a busy feed (hundreds of electric
        # filings/month). A normally-productive window returning zero is logged loudly,
        # never swallowed (the dormant-IMM / NJ-backfill lesson).
        if not filings:
            logger.warning(
                "VaSccAdapter: ZERO electric filings for %s..%s (raw_rows=%d, watch=%d) — "
                "verify the Breeze feed is live and the parser still matches "
                "(GetAllDailyFilings / CaseName / Year-Month-Day / FileName)",
                since_date,
                today,
                len(rows),
                len(self._watch),
            )
        else:
            logger.info(
                "VaSccAdapter: %d electric filings (%d raw rows in window) %s..%s",
                len(filings),
                len(rows),
                since_date,
                today,
            )
        return filings
