"""MD PSC (Maryland Public Service Commission) DMS official-filings adapter.

Source: https://webpscxb.pscmaryland.com/DMS/official-filings — ASP.NET WebForms
behind Cloudflare. One flat, all-industry mail-log firehose. NO industry filter, and
case numbers (4-digit, e.g. 9681) carry no industry code, and Commission ORDERS are
filed by "Commission" (not the utility). So a company-name filter alone would miss
the orders — electric scope must key off the CASE.

Strategy (case-scoped — CpucAdapter's persistent watch set + an in-window discovery
feeder folded together):
- Pull the date-range firehose (server-rendered `maillogdata` table; the whole window
  returns in ONE POST, no pagination — we chunk by ~month so backfills stay small).
- electric_cases = Case Nos that an electric utility filed in *this window* UNION the
  persistent DB watch set (dockets jurisdiction='MD-PSC', passed in by the handler).
- Keep a row if its Case No. is an electric case OR its filer is an electric utility
  (the latter catches utility reports/tariffs/waivers that carry no case number).
This captures utility filings + Commission ORDERS + intervener/staff filings in
electric cases — exactly the orders a company-name filter would drop.

Row shape: each `<tr>` = a button cell (`data-pdf='/DMS/maillogpdfview/MailLog/0/0/
{maillog}/0'` + `ML# {maillog}`) and a free-text description cell:
  "{filer} filed, on {Month DD, YYYY}, {desc} [... Case No. NNNN ...]".
external_id = MailLog number; source_url = the maillogpdfview URL; deferred R2.
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_BASE_URL = "https://webpscxb.pscmaryland.com"
_URL = f"{_BASE_URL}/DMS/official-filings"
_F = "ctl00$ContentPlaceHolder1$"
_ET = ZoneInfo("America/New_York")
_WINDOW_DAYS = 31  # firehose returns the whole window in one POST; chunk to bound size

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 NodalPulse/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Electric utilities regulated by MD PSC — lowercased substrings matched against the
# filer (the text before " filed,"). A row by one of these is electric even with no
# case number; their case numbers seed the electric-case set for orders/interveners.
_ELECTRIC_FILERS = (
    "potomac electric power",  # Pepco
    "baltimore gas and electric",  # BGE
    "delmarva power",  # Delmarva Power & Light
    "potomac edison",  # The Potomac Edison Company
    "southern maryland electric",  # SMECO
    "choptank electric",  # Choptank Electric Cooperative
)

_DOC_TYPE_MAP: list[tuple[str, str]] = [
    ("order", "mdpsc-order"),
    ("application", "mdpsc-application"),
    ("tariff", "mdpsc-tariff"),
    ("complaint", "mdpsc-complaint"),
    ("petition", "mdpsc-petition"),
    ("comment", "mdpsc-comments"),
    ("testimony", "mdpsc-testimony"),
    ("settlement", "mdpsc-settlement"),
    ("stipulation", "mdpsc-settlement"),
    ("motion", "mdpsc-motion"),
    ("notice", "mdpsc-notice"),
    ("report", "mdpsc-report"),
    ("letter", "mdpsc-correspondence"),
    ("correspondence", "mdpsc-correspondence"),
]

# ── parsing helpers (pure → hermetically testable) ────────────────────────────────

_VS_RE = re.compile(r'id="__VIEWSTATE"\s+value="([^"]*)"')
_VSG_RE = re.compile(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"')
_TBODY_RE = re.compile(r'id="maillogdata".*?<tbody>(.*?)</tbody>', re.S)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_ML_RE = re.compile(r"data-pdf='([^']+)'[^>]*>\s*ML#\s*(\d+)", re.I)
_DATE_RE = re.compile(r"filed,\s+on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.I)
_CASE_RE = re.compile(r"Case\s+No\.?\s*(\d{3,5})", re.I)


def extract_viewstate(html: str) -> dict[str, str]:
    """MD WebForms uses __VIEWSTATE + __VIEWSTATEGENERATOR (no __EVENTVALIDATION)."""
    return {
        "__VIEWSTATE": (m.group(1) if (m := _VS_RE.search(html)) else ""),
        "__VIEWSTATEGENERATOR": (m.group(1) if (m := _VSG_RE.search(html)) else ""),
    }


def _parse_md_date(raw: str) -> str | None:
    """'June 30, 2026' (Eastern) → UTC ISO-8601 (midnight ET)."""
    try:
        naive = datetime.strptime(raw.strip(), "%B %d, %Y")
    except ValueError:
        return None
    return naive.replace(tzinfo=_ET).astimezone(UTC).isoformat()


def _doc_type(description: str) -> str:
    d = description.lower()
    for key, val in _DOC_TYPE_MAP:
        if key in d:
            return val
    return "mdpsc-filing"


def is_electric_filer(filer: str) -> bool:
    f = filer.lower()
    return any(e in f for e in _ELECTRIC_FILERS)


def parse_rows(html: str) -> list[dict]:
    """Parse the maillogdata tbody → row dicts. Pure: no network, no filtering."""
    tb = _TBODY_RE.search(html)
    if not tb:
        return []
    rows: list[dict] = []
    for tr in _ROW_RE.findall(tb.group(1)):
        cells = _CELL_RE.findall(tr)
        if len(cells) < 2:
            continue
        ml = _ML_RE.search(cells[0])
        if not ml:
            continue
        pdf_path, maillog = ml.group(1), ml.group(2)
        desc = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cells[1])).strip())
        filer = desc.split(" filed,")[0].strip() if " filed," in desc else ""
        dm = _DATE_RE.search(desc)
        filed_at = _parse_md_date(dm.group(1)) if dm else None
        if not filed_at:
            continue
        cm = _CASE_RE.search(desc)
        rows.append(
            {
                "maillog": maillog,
                "pdf_path": pdf_path,
                "filer": filer,
                "description": desc,
                "case": cm.group(1) if cm else "",
                "filed_at": filed_at,
            }
        )
    return rows


def _date_windows(start: date, end: date, days: int = _WINDOW_DAYS) -> list[tuple[date, date]]:
    """Split [start, end] into ≤`days`-long (from, to) windows."""
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        out.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return out


def _to_filing(row: dict) -> RawFiling:
    return RawFiling(
        source_slug="mdpsc",
        external_id=row["maillog"],
        doc_type=_doc_type(row["description"]),
        title=row["description"][:500],
        source_url=f"{_BASE_URL}{row['pdf_path']}",
        filed_at=row["filed_at"],
        content=b"",  # deferred R2 — extract worker fetches source_url post-triage
        file_ext="pdf",
        metadata={
            "docket_numbers": [row["case"]] if row["case"] else [],
            "filer": row["filer"],
            "description_raw": row["description"],
            "maillog": row["maillog"],
            "posted_date": row["filed_at"][:10],
        },
    )


# ── adapter ───────────────────────────────────────────────────────────────────────


class MdPscAdapter(MarketAdapter):
    """MD PSC DMS adapter — case-scoped electric coverage over the mail-log firehose.

    Args:
        watch_cases: MD-PSC case numbers already known to be electric (from the dockets
            table). Unioned with cases discovered in the crawl window so orders /
            intervener filings in known electric cases are captured across windows.
    """

    source_slug = "mdpsc"

    def __init__(self, watch_cases: set[str]) -> None:
        self._watch = {c.strip() for c in watch_cases if c and c.strip()}

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )
        today = date.today()
        windows = _date_windows(since_date, today)
        logger.info(
            "MdPscAdapter: %s..%s (%d windows, watch_set=%d)",
            since_date,
            today,
            len(windows),
            len(self._watch),
        )

        all_rows: list[dict] = []
        async with httpx.AsyncClient(
            base_url=_BASE_URL, follow_redirects=True, timeout=90, headers=_HEADERS
        ) as client:
            for w_from, w_to in windows:
                try:
                    all_rows.extend(await self._search(client, w_from, w_to))
                except Exception:
                    logger.exception("MdPscAdapter: window %s..%s failed", w_from, w_to)

        return self._finalize(all_rows, since_date, today)

    async def _search(self, client: httpx.AsyncClient, w_from: date, w_to: date) -> list[dict]:
        """GET viewstate, POST a date-range firehose search, parse all rows (no paging)."""
        form = await client.get(_URL)
        form.raise_for_status()
        vs = extract_viewstate(form.text)
        if not vs["__VIEWSTATE"]:
            logger.error("MdPscAdapter: viewstate missing from %s — site drift?", _URL)
            return []
        payload = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            **vs,
            f"{_F}txtStartDate": w_from.isoformat(),
            f"{_F}txtEndDate": w_to.isoformat(),
            f"{_F}txtCompanyName": "",
            f"{_F}txtMailLogNum": "",
            f"{_F}btnSubmit": "Submit",
        }
        res = await client.post(_URL, data=payload, headers={"Origin": _BASE_URL, "Referer": _URL})
        res.raise_for_status()
        rows = parse_rows(res.text)
        logger.info("MdPscAdapter: %s..%s → %d raw rows", w_from, w_to, len(rows))
        return rows

    def _finalize(self, rows: list[dict], since_date: date, today: date) -> list[RawFiling]:
        """Discover electric cases (utility filers + DB watch set), keep electric rows."""
        electric_cases = set(self._watch)
        for r in rows:
            if r["case"] and is_electric_filer(r["filer"]):
                electric_cases.add(r["case"])

        seen: set[str] = set()
        keep: list[dict] = []
        for r in rows:
            if r["maillog"] in seen:
                continue
            seen.add(r["maillog"])
            if (r["case"] and r["case"] in electric_cases) or is_electric_filer(r["filer"]):
                keep.append(r)

        filings = [_to_filing(r) for r in keep]

        # Anti-silent-zero floor: a normally-productive crawl returning zero is logged
        # loudly, never swallowed. (The dormant-IMM / NJ-backfill lesson.)
        if not filings:
            logger.warning(
                "MdPscAdapter: ZERO electric filings for %s..%s (raw_rows=%d, "
                "electric_cases=%d) — verify the firehose is live and the parser still "
                "matches (maillogdata / ML# / 'filed, on <date>' / Case No.)",
                since_date,
                today,
                len(rows),
                len(electric_cases),
            )
        else:
            logger.info(
                "MdPscAdapter: %d electric filings (%d raw rows, %d electric cases) %s..%s",
                len(filings),
                len(rows),
                len(electric_cases),
                since_date,
                today,
            )
        return filings
