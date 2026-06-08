"""CPUC (California Public Utilities Commission) document search adapter.

Source: https://docs.cpuc.ca.gov/advancedsearchform.aspx
Mechanism: ASP.NET WebForms — GET for session/viewstate, then POST per proceeding.
No REST/JSON layer exists behind the form.

Session management:
- One GET per crawl tick (shared across all watched proceedings)
- ASP.NET_SessionId cookie + __VIEWSTATE reused for all POSTs in the tick
- sleep(1) between per-proceeding queries (no rate-limit signs detected; polite crawl)

Sort order:
- Default: PubDate DESC (confirmed empirically: monotone within and across pages)
- Early-stop: when page tail < since_date, all remaining pages are also older

PDF strategy (deferred R2):
- source_url = direct stable URL at crawl time (https://docs.cpuc.ca.gov/PublishedDocs/...)
- content = b"" — PDF fetched at extraction time post-triage (same pattern as PUCT)

External ID:
- Numeric document ID from PDF filename: /PublishedDocs/.../608275753.pdf → "608275753"
- Unique per document; stable across repeated fetches

Proceeding number format:
- Search form uses no separators: A2508008, R2106017
- Cross-refs from CAISO extractions use A.YY-MM-NNN; normalized before query via _normalize_proc()

Testimony:
- ddlEfileTypes=143 returns 0 results for all Energy searches — confirmed empirically.
- Testimonies are filed with the ALJ directly and not indexed in docs.cpuc.ca.gov.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_FORM_URL = "https://docs.cpuc.ca.gov/advancedsearchform.aspx"
_RESULTS_URL = "https://docs.cpuc.ca.gov/SearchRes.aspx"
_BASE_URL = "https://docs.cpuc.ca.gov"
_RESULTS_PER_PAGE = 20
_MAX_PAGES_PER_PROC = 50  # hard cap; 50×20=1000 docs per proceeding

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 NodalPulse/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_LA_TZ = ZoneInfo("America/Los_Angeles")

# Viewstate / validation patterns — id="..." form (the CPUC form uses both)
_VS_RE  = re.compile(r'id="__VIEWSTATE"\s+[^>]*value="([^"]*)"')
_VSG_RE = re.compile(r'id="__VIEWSTATEGENERATOR"\s+[^>]*value="([^"]*)"')
_EV_RE  = re.compile(r'id="__EVENTVALIDATION"\s+[^>]*value="([^"]*)"')
_NUM_RESULTS_RE = re.compile(r'var numResults\s*=\s*"(\d+)"')

# PDF href (single or double-quoted, any /PublishedDocs path)
_PDF_HREF_RE = re.compile(r"""href=['"]?(/PublishedDocs[^'">\s]+)['"]?""", re.I)
# Numeric doc ID from PDF filename: .../608275753.pdf → "608275753"
_PDF_ID_RE   = re.compile(r"/([0-9]+)\.\w{2,5}$", re.I)
# Proceeding embedded in title cell
_PROC_IN_TITLE_RE = re.compile(r"Proceeding:\s*([A-Za-z][0-9]+)", re.I)
# Strip leading proceeding number prefix from title text
_PROC_PREFIX_RE   = re.compile(r"^[A-Z][0-9]{5,9}\s+")
# Normalize proceeding number: A.25-08-008 → A2508008
_PROC_NORM_RE     = re.compile(r"[.\-\s]")
# Valid normalized proc: letter then ≥5 digits
_PROC_VALID_RE    = re.compile(r"^[A-Z][0-9]{5,}$")


def normalize_proc(raw: str) -> str:
    """Strip dots, dashes, spaces and uppercase — convert A.25-08-008 → A2508008."""
    return _PROC_NORM_RE.sub("", raw.strip().upper())


# Ordered map (most-specific key first)
_DOC_TYPE_MAP: list[tuple[str, str]] = [
    ("proposed decision",            "cpuc-proposed-decision"),
    ("presiding officers decision",  "cpuc-proposed-decision"),
    ("draft decision",               "cpuc-proposed-decision"),
    ("final decision",               "cpuc-decision"),
    ("agenda decision",              "cpuc-decision"),
    ("comment decision",             "cpuc-decision"),
    ("final resolution",             "cpuc-resolution"),
    ("agenda resolution",            "cpuc-resolution"),
    ("comment resolution",           "cpuc-resolution"),
    ("alj resolution",               "cpuc-resolution"),
    ("scoping ruling",               "cpuc-ruling"),
    ("ruling",                       "cpuc-ruling"),
    ("compliance filing",            "cpuc-filing"),
    ("supporting document",          "cpuc-filing"),
    ("comments",                     "cpuc-comments"),
    ("motion",                       "cpuc-motion"),
    ("petition",                     "cpuc-petition"),
    ("application",                  "cpuc-application"),
    ("complaint",                    "cpuc-complaint"),
    ("exparte",                      "cpuc-exparte"),
    ("notice",                       "cpuc-notice"),
    ("order",                        "cpuc-order"),
    ("testimony",                    "cpuc-testimony"),
    ("exhibit",                      "cpuc-filing"),
    ("brief",                        "cpuc-brief"),
    ("protest",                      "cpuc-protest"),
    ("answer",                       "cpuc-filing"),
    ("amendment",                    "cpuc-filing"),
    ("supplement",                   "cpuc-filing"),
    ("report",                       "cpuc-informational"),
    ("federal filings",              "cpuc-informational"),
    ("response",                     "cpuc-filing"),
    ("reply",                        "cpuc-filing"),
]


def _infer_doc_type(raw: str) -> str:
    cleaned = re.sub(r"^e-?filed\s*:\s*", "", raw, flags=re.I).strip().lower()
    for key, val in _DOC_TYPE_MAP:
        if key in cleaned:
            return val
    return "cpuc-filing"


def _ext_from_path(path: str) -> str:
    m = re.search(r"\.(\w{2,5})$", path, re.I)
    return m.group(1).lower() if m else "pdf"


# ── session management ────────────────────────────────────────────────────────


async def _init_session(client: httpx.AsyncClient) -> dict | None:
    """GET the search form; return {vs, vsg, ev} for reuse across all proceedings."""
    try:
        r = await client.get(_FORM_URL)
        r.raise_for_status()
    except Exception:
        logger.exception("CpucAdapter: GET form page failed")
        return None

    vs  = _VS_RE.search(r.text)
    vsg = _VSG_RE.search(r.text)
    ev  = _EV_RE.search(r.text)

    if not vs or not ev:
        logger.error("CpucAdapter: __VIEWSTATE / __EVENTVALIDATION missing from form page")
        return None

    return {"vs": vs.group(1), "vsg": vsg.group(1) if vsg else "", "ev": ev.group(1)}


def _extract_session(html: str) -> dict | None:
    """Pull viewstate from a results-page response (used for pagination)."""
    vs  = _VS_RE.search(html)
    vsg = _VSG_RE.search(html)
    ev  = _EV_RE.search(html)
    if not vs or not ev:
        return None
    return {"vs": vs.group(1), "vsg": vsg.group(1) if vsg else "", "ev": ev.group(1)}


# ── HTTP calls ────────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _post_search(
    client: httpx.AsyncClient,
    session: dict,
    proc: str,
    since_date: date,
) -> httpx.Response:
    """POST search form for one proceeding with PubDate range filter."""
    r = await client.post(
        _FORM_URL,
        data={
            "__EVENTTARGET":      "",
            "__EVENTARGUMENT":    "",
            "__VIEWSTATE":        session["vs"],
            "__VIEWSTATEGENERATOR": session["vsg"],
            "__EVENTVALIDATION":  session["ev"],
            "DocTitle":           "",
            "ddlCpuc01Types":     "-1",
            "ddlEfileTypes":      "-1",
            "IndustryID":         "1",  # Energy
            "ProcNum":            proc,
            "MeetDate":           "",
            "PubDateFrom":        since_date.strftime("%m/%d/%Y"),
            "PubDateTo":          date.today().strftime("%m/%d/%Y"),
            "EfileConfirmNum":    "",
            "FilingDateFrom":     "",
            "FilingDateTo":       "",
            "SearchButton":       "Search",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": _FORM_URL},
    )
    r.raise_for_status()
    return r


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _post_next_page(
    client: httpx.AsyncClient,
    page_session: dict,
) -> httpx.Response:
    """POST to SearchRes.aspx to retrieve the next result page."""
    r = await client.post(
        _RESULTS_URL,
        data={
            "__EVENTTARGET":        "lnkNextPage",
            "__EVENTARGUMENT":      "",
            "__VIEWSTATE":          page_session["vs"],
            "__VIEWSTATEGENERATOR": page_session["vsg"],
            "__EVENTVALIDATION":    page_session["ev"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": _FORM_URL},
    )
    r.raise_for_status()
    return r


# ── HTML parsing ──────────────────────────────────────────────────────────────


def _parse_result_page(
    html: str,
    proc: str,
    since_date: date,
) -> tuple[list[RawFiling], bool]:
    """Parse one page of search results.

    Returns (filings, should_stop).
    should_stop=True when the last row on the page has PubDate < since_date.
    Server-side PubDateFrom filter normally handles this, but this is the safety gate.
    """
    tbl = re.search(r'id="ResultTable"[^>]*>(.*?)</table>', html, re.I | re.S)
    if not tbl:
        return [], True

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(1), re.I | re.S)
    filings: list[RawFiling] = []
    last_date: date | None = None

    for row in rows:
        if "ResultTitleTD" not in row:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) < 4:
            continue

        title_raw    = re.sub(r"<[^>]+>", " ", cells[0]).strip()
        title_raw    = re.sub(r"\s+", " ", title_raw)
        doc_type_raw = re.sub(r"<[^>]+>", "", cells[1]).strip()
        pub_date_str = re.sub(r"<[^>]+>", "", cells[3]).strip()

        pdf_href = _PDF_HREF_RE.search(cells[2]) if len(cells) > 2 else None
        if not pdf_href:
            continue
        pdf_path   = pdf_href.group(1)
        source_url = _BASE_URL + pdf_path

        id_match = _PDF_ID_RE.search(pdf_path)
        if not id_match:
            continue
        external_id = id_match.group(1)

        try:
            pub_date = datetime.strptime(pub_date_str, "%m/%d/%Y").date()
        except ValueError:
            logger.warning("CpucAdapter: unparseable date %r in proc %s", pub_date_str, proc)
            continue

        last_date = pub_date

        if pub_date < since_date:
            continue

        # Proceeding from title (may differ from queried proc for cross-listed docs)
        proc_in_title = _PROC_IN_TITLE_RE.search(title_raw)
        filing_proc   = proc_in_title.group(1) if proc_in_title else proc

        # Clean title: strip "Proceeding: XXXX" suffix
        title = re.sub(r"\s*Proceeding:\s*\S+\s*$", "", title_raw).strip()
        if not title:
            title = f"{doc_type_raw} — {filing_proc}"

        # Filer: text after the leading proceeding-number prefix in the title
        filer = ""
        title_no_prefix = _PROC_PREFIX_RE.sub("", title)
        if title_no_prefix and title_no_prefix != title:
            filer = title_no_prefix[:120]

        filed_at = datetime(pub_date.year, pub_date.month, pub_date.day,
                            tzinfo=_LA_TZ).isoformat()

        filings.append(RawFiling(
            source_slug="cpuc",
            external_id=external_id,
            doc_type=_infer_doc_type(doc_type_raw),
            title=title[:500],
            source_url=source_url,
            filed_at=filed_at,
            content=b"",    # deferred R2 — fetched at extraction time post-triage
            file_ext=_ext_from_path(pdf_path),
            metadata={
                "proc_num":      filing_proc,
                "doc_type_raw":  doc_type_raw,
                "pub_date":      pub_date.isoformat(),
                "docket_numbers": [filing_proc],
                "filer":         filer,
            },
        ))

    # Early-stop: last row pub_date < since_date.
    # numResults in the JS reflects the server-side PubDateFrom-filtered count (confirmed
    # empirically: R2106017 + 06/01 date range → numResults=20, not the proc's historic total).
    # So len(all_filings) < total in the caller is a reliable last-page guard; date_stop is
    # a belt-and-suspenders safety net for the unlikely case the server filter misfires.
    date_stop = last_date is not None and last_date < since_date
    return filings, date_stop


# ── per-proceeding fetch ──────────────────────────────────────────────────────


async def _fetch_proceeding(
    client: httpx.AsyncClient,
    session: dict,
    proc: str,
    since_date: date,
) -> list[RawFiling]:
    """Fetch all result pages for one proceeding since since_date.

    Stops when:
    - A page has fewer than _RESULTS_PER_PAGE data rows (last page)
    - The date-stop signal fires (tail of page is before since_date)
    - The page cap is reached
    - Pagination returns a 500 (server-side session state lost — return what we have)
    """
    r = await _post_search(client, session, proc, since_date)

    n_match = _NUM_RESULTS_RE.search(r.text)
    total = int(n_match.group(1)) if n_match else 0

    if total == 0:
        logger.info("CpucAdapter: proc=%s → 0 docs since=%s", proc, since_date)
        return []

    logger.info("CpucAdapter: proc=%s reported_total=%d since=%s", proc, total, since_date)

    all_filings: list[RawFiling] = []
    filings, should_stop = _parse_result_page(r.text, proc, since_date)
    all_filings.extend(filings)

    page = 1
    while not should_stop and len(all_filings) < total and page < _MAX_PAGES_PER_PROC:
        page_session = _extract_session(r.text)
        if not page_session:
            logger.warning("CpucAdapter: proc=%s page=%d lost viewstate — stopping", proc, page)
            break

        await asyncio.sleep(0.5)
        try:
            r = await _post_next_page(client, page_session)
        except Exception as exc:
            # Server-side session expiry or 500 — return what we have from earlier pages.
            logger.warning(
                "CpucAdapter: proc=%s page=%d pagination error (%s) — returning %d filings from prior pages",
                proc, page, exc, len(all_filings),
            )
            if len(all_filings) < total:
                logger.warning(
                    "CpucAdapter: proc=%s POSSIBLE DOC LOSS — got %d of server-reported %d; "
                    "CPUC SearchRes.aspx 500 may have dropped pages",
                    proc, len(all_filings), total,
                )
            break
        filings, should_stop = _parse_result_page(r.text, proc, since_date)
        all_filings.extend(filings)
        page += 1

    if page >= _MAX_PAGES_PER_PROC:
        logger.warning(
            "CpucAdapter: proc=%s hit page cap (%d) — got %d of server-reported %d",
            proc, _MAX_PAGES_PER_PROC, len(all_filings), total,
        )

    return all_filings


# ── adapter ───────────────────────────────────────────────────────────────────


class CpucAdapter(MarketAdapter):
    """CPUC docs.cpuc.ca.gov adapter.

    Queries one proceeding at a time (watch-set governor). Session (cookie +
    viewstate) is initialized once per tick and shared across all proceedings.

    Args:
        proc_numbers: CPUC proceeding IDs in any format. Dots/dashes are
                      normalized — A.25-08-008 and A2508008 both work.
    """

    source_slug = "cpuc"

    def __init__(self, proc_numbers: set[str]) -> None:
        normalized = {normalize_proc(p) for p in proc_numbers if p.strip()}
        self._watched: set[str] = {p for p in normalized if _PROC_VALID_RE.match(p)}
        skipped = len(proc_numbers) - len(self._watched)
        if skipped:
            logger.debug("CpucAdapter: skipped %d malformed proc numbers", skipped)

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )

        if not self._watched:
            logger.info("CpucAdapter: watch set empty — no proceedings to poll")
            return []

        logger.info("CpucAdapter: %d proceedings since=%s", len(self._watched), since_date)

        filings: list[RawFiling] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=60,
            headers=_HEADERS,
        ) as client:
            session = await _init_session(client)
            if not session:
                logger.error("CpucAdapter: session init failed — aborting tick")
                return []

            for proc in sorted(self._watched):
                await asyncio.sleep(1)  # polite inter-proceeding delay
                try:
                    proc_filings = await _fetch_proceeding(client, session, proc, since_date)
                    for f in proc_filings:
                        if f.external_id not in seen_ids:
                            filings.append(f)
                            seen_ids.add(f.external_id)
                except Exception:
                    logger.exception("CpucAdapter: error on proc=%s", proc)

        logger.info(
            "CpucAdapter: %d new filings across %d proceedings",
            len(filings), len(self._watched),
        )
        return filings
