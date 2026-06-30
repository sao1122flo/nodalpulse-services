"""NJ BPU (New Jersey Board of Public Utilities) public-document adapter.

FIRST state-PUC adapter of PJM Wave 1 — built as the **reusable reference** for the
sibling portals (MD PSC, PA PUC, VA SCC) and later state waves. The shape here is
the template: GET form → carry viewstate → POST a date-range firehose → parse the
server-rendered results grid → paginate → emit deferred-R2 RawFilings. Keep new
state adapters this close to it; what is clean here makes the next 12+ states cheap.

Source: https://publicaccess.bpu.state.nj.us/Search.aspx
Mechanism: ASP.NET WebForms behind Imperva Incapsula. No REST/JSON layer.

THE NON-OBVIOUS BIT (cost an hour to find — documented so the clones don't repeat it):
- A faithful viewstate POST is rejected by the origin with Error.aspx
  ("Server cannot process this request") UNLESS an `Origin` header is sent on the
  POST. Incapsula gates state-changing POSTs on a matching Origin. This is the one
  header CpucAdapter didn't need; every Incapsula-fronted WebForms portal will.
- Incapsula cookies (visid_incap / nlbi / incap_ses) arrive via Set-Cookie HEADERS
  (not JS), so an httpx cookie jar captures them with a plain warm-up GET. No
  browser / JS execution required.
- ASP.NET_SessionId rotates every request (empty session) — harmless here.

Firehose:
- searchType=Advanced + OpenDateFrom/OpenDateTo (MM/DD/YYYY) + ListType=Document.
- OpenDate filters on the document's POSTED date → a true new-filings-by-date feed
  (not case-open date), so the rolling `since` window behaves like every other source.

Results grid (id=ContentPlaceHolder1_gvSearchRs), page size 30, total in
ContentPlaceHolder1_lCount ("1 - 30 of 114"). Columns:
  Docket# (CaseSummary.aspx?case_id=N → "ER23120924-") | Document Title
  (DocumentHandler.ashx?document_id=N) | Folder (=doc type) | Uploaded By |
  Description | Posted Date (MM/DD/YYYY) | Fragment (keyword snippet only).

External ID / verify-link:
- external_id = document_id; source_url = .../DocumentHandler.ashx?document_id=N
  (the stable per-document URL, also the B4 "verify" link surfaced to users).

PDF strategy (deferred R2, same as CPUC/PUCT/FERC):
- content = b"" at crawl time; the extract worker fetches source_url post-triage.

Electric scope:
- NJ docket prefixes encode industry (E*=electric, G*=gas, W*=water, T*=telecom,
  QO/QX=clean energy). We keep E* + clean-energy and DROP the rest — and log how many
  were dropped, so the electric scope is transparent rather than a silent filter.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_BASE_URL = "https://publicaccess.bpu.state.nj.us"
_SEARCH_URL = f"{_BASE_URL}/Search.aspx"
_RESULTS_URL = f"{_BASE_URL}/SearchDocResults.aspx"
_DOC_URL = f"{_BASE_URL}/DocumentHandler.ashx?document_id="
_PAGE_SIZE = 30
_MAX_PAGES = 60  # hard cap per prefix: 60×30 = 1800 docs
_PAGE_DELAY = 1.5  # polite delay between pagination POSTs — Incapsula throttles rapid
# datacenter-IP POSTs (a 4th rapid POST hung 60s in prod); the delay + retry avoid it.

_ET = ZoneInfo("America/New_York")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 NodalPulse/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# WebForms control-name prefix for the search filter user control.
_F = "ctl00$ContentPlaceHolder1$searchFilter$"

# Electric / clean-energy docket prefixes (industry is encoded in the docket number).
# E* = electric (ER rate, EO other, EM misc, EA, EE); QO/QX = clean-energy (solar,
# storage, OSW) which is grid-relevant. G*/W*/T* (gas/water/telecom) are dropped.
_ELECTRIC_PREFIXES = ("E", "QO", "QX")

# Server-side electric filter: the Advanced AdvanceCaseNumber field does a docket
# PREFIX match (verified: "EO" → only EO*; a single "E" is too broad — it substring-
# matches "CE..."). So we query each 2-letter electric prefix and union the results,
# instead of pulling the whole cross-industry firehose and filtering client-side. That
# was the bug a backfill exposed: the grid's default sort is docket-ascending, so over a
# wide window the E* dockets sort *after* hundreds of A*/C*/G* rows — pagination died
# before reaching them and the client filter dropped every row it had. Querying by prefix
# means every fetched row is already in scope. Nonexistent prefixes harmlessly return 0.
_QUERY_PREFIXES = ("ER", "EO", "EM", "EE", "EF", "ET", "EW", "EA", "EX", "QO")

# Folder (DMS document category) → normalized doc_type. Ordered: first substring wins.
_DOC_TYPE_MAP: list[tuple[str, str]] = [
    ("order", "njbpu-order"),
    ("decision", "njbpu-order"),
    ("petition", "njbpu-petition"),
    ("application", "njbpu-application"),
    ("testimony", "njbpu-testimony"),
    ("brief", "njbpu-brief"),
    ("comment", "njbpu-comments"),
    ("motion", "njbpu-motion"),
    ("tariff", "njbpu-tariff"),
    ("rulemaking", "njbpu-rulemaking"),
    ("rule", "njbpu-rulemaking"),
    ("stipulation", "njbpu-settlement"),
    ("settlement", "njbpu-settlement"),
    ("notice", "njbpu-notice"),
    ("correspondence", "njbpu-correspondence"),
    ("letter", "njbpu-correspondence"),
    ("report", "njbpu-report"),
    ("exhibit", "njbpu-filing"),
    ("compliance", "njbpu-filing"),
]

# ── viewstate / parsing helpers (module-level, pure → hermetically testable) ────

_VS_RE = re.compile(r'id="__VIEWSTATE"\s+value="([^"]*)"')
_VSG_RE = re.compile(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"')
_VSE_RE = re.compile(r'id="__VIEWSTATEENCRYPTED"\s+value="([^"]*)"')
_EV_RE = re.compile(r'id="__EVENTVALIDATION"\s+value="([^"]*)"')
# Pager "Next" link target, e.g. ctl00$ContentPlaceHolder1$gvSearchRs$ctl33$lbtnNext.
# The ctlNN index shifts on the last (short) page, so extract it per page.
_NEXT_RE = re.compile(r"__doPostBack\(&#39;([^&]*gvSearchRs\$ctl\d+\$lbtnNext)&#39;")
# Total result count: "1 - 30 of 114".
_COUNT_RE = re.compile(r'id="ContentPlaceHolder1_lCount">\s*[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)')
_CASE_ID_RE = re.compile(r"case_id=(\d+)")
_DOC_ID_RE = re.compile(r"document_id=(\d+)")


def extract_viewstate(html: str) -> dict[str, str]:
    """Pull the ASP.NET hidden postback fields needed to re-POST the form."""
    return {
        "__VIEWSTATE": (m.group(1) if (m := _VS_RE.search(html)) else ""),
        "__VIEWSTATEGENERATOR": (m.group(1) if (m := _VSG_RE.search(html)) else ""),
        "__VIEWSTATEENCRYPTED": (m.group(1) if (m := _VSE_RE.search(html)) else ""),
        "__EVENTVALIDATION": (m.group(1) if (m := _EV_RE.search(html)) else ""),
    }


def parse_total(html: str) -> int | None:
    """Total result count from the lCount span; None if the marker is absent."""
    m = _COUNT_RE.search(html)
    return int(m.group(1).replace(",", "")) if m else None


def next_page_target(html: str) -> str | None:
    """The __doPostBack target of the pager 'Next' link, or None on the last page."""
    m = _NEXT_RE.search(html)
    return m.group(1) if m else None


def is_electric(docket: str) -> bool:
    return docket.upper().startswith(_ELECTRIC_PREFIXES)


def _doc_type(folder: str) -> str:
    f = folder.strip().lower()
    for key, val in _DOC_TYPE_MAP:
        if key in f:
            return val
    return "njbpu-filing"


def _parse_date(raw: str) -> str | None:
    """NJ posted date MM/DD/YYYY (Eastern) → UTC ISO-8601 (midnight ET)."""
    try:
        naive = datetime.strptime(raw.strip(), "%m/%d/%Y")
    except ValueError:
        return None
    return naive.replace(tzinfo=_ET).astimezone(UTC).isoformat()


def _cell_text(node) -> str:
    return re.sub(r"\s+", " ", node.text(strip=True).replace("\xa0", " ")).strip() if node else ""


def parse_results(html: str) -> list[dict]:
    """Parse one gvSearchRs page → row dicts. Pure: no network, no filtering.

    Returns [] when the results table is absent (the caller treats that as a
    structure-drift signal, not silently as "no new filings").
    """
    tree = HTMLParser(html)
    table = tree.css_first("#ContentPlaceHolder1_gvSearchRs")
    if table is None:
        return []

    rows: list[dict] = []
    for tr in table.css("tr"):
        cells = tr.css("td")
        if len(cells) < 6:
            continue  # header row (th) or pager row

        docket_link = cells[0].css_first("a")
        title_link = cells[1].css_first("a[href*='DocumentHandler']")
        if title_link is None:
            continue

        href = title_link.attributes.get("href", "") or ""
        doc_id_m = _DOC_ID_RE.search(href)
        if not doc_id_m:
            continue
        document_id = doc_id_m.group(1)

        docket_raw = _cell_text(docket_link) if docket_link else ""
        docket = docket_raw.rstrip("-").strip()
        case_id = ""
        if docket_link is not None:
            cm = _CASE_ID_RE.search(docket_link.attributes.get("href", "") or "")
            case_id = cm.group(1) if cm else ""

        filed_at = _parse_date(_cell_text(cells[5]))
        if not filed_at:
            continue

        rows.append(
            {
                "document_id": document_id,
                "docket": docket,
                "case_id": case_id,
                "title": _cell_text(title_link),
                "folder": _cell_text(cells[2]),
                "uploaded_by": _cell_text(cells[3]),
                "description": _cell_text(cells[4]),
                "filed_at": filed_at,
            }
        )
    return rows


def _to_filing(row: dict) -> RawFiling:
    title = row["title"] or row["description"] or f"{row['folder']} — {row['docket']}"
    return RawFiling(
        source_slug="njbpu",
        external_id=row["document_id"],
        doc_type=_doc_type(row["folder"]),
        title=title[:500],
        source_url=f"{_DOC_URL}{row['document_id']}",
        filed_at=row["filed_at"],
        content=b"",  # deferred R2 — extract worker fetches source_url post-triage
        file_ext="pdf",  # DocumentHandler serves the stored file; PDF is the norm
        metadata={
            "docket_numbers": [row["docket"]] if row["docket"] else [],
            "case_id": row["case_id"],
            "folder": row["folder"],
            "filer": row["uploaded_by"],
            "description_raw": row["description"],
            "posted_date": row["filed_at"][:10],
        },
    )


# ── adapter ─────────────────────────────────────────────────────────────────────


class NjBpuAdapter(MarketAdapter):
    """NJ BPU public-document adapter — electric-scoped, server-side filtered.

    Not a raw firehose (that buried electric dockets behind a docket-ascending sort
    and died on pagination). Instead, one Advanced search per electric docket prefix
    (AdvanceCaseNumber prefix match) over the posted-date window, unioned. Like
    CpucAdapter it reuses one Incapsula/session warm-up; unlike it there is no watch
    set — the electric prefix list is the scope.
    """

    source_slug = "njbpu"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )
        d_from = since_date.strftime("%m/%d/%Y")
        d_to = date.today().strftime("%m/%d/%Y")
        logger.info("NjBpuAdapter: %s..%s prefixes=%s", d_from, d_to, list(_QUERY_PREFIXES))

        all_rows: list[dict] = []
        async with httpx.AsyncClient(
            base_url=_BASE_URL, follow_redirects=True, timeout=60, headers=_HEADERS
        ) as client:
            # Warm-up GET establishes Incapsula + session cookies (via Set-Cookie headers).
            try:
                await client.get(_SEARCH_URL)
            except Exception:
                logger.exception("NjBpuAdapter: warm-up GET failed — aborting tick")
                return []

            for prefix in _QUERY_PREFIXES:
                try:
                    rows = await self._search_prefix(client, prefix, d_from, d_to)
                    all_rows.extend(rows)
                except Exception:
                    logger.exception("NjBpuAdapter: prefix %s failed", prefix)

        return self._finalize(all_rows, d_from, d_to)

    async def _search_prefix(
        self, client: httpx.AsyncClient, prefix: str, d_from: str, d_to: str
    ) -> list[dict]:
        """GET fresh viewstate, POST an Advanced docket-prefix search, paginate."""
        form = await client.get(_SEARCH_URL)
        form.raise_for_status()
        vs = extract_viewstate(form.text)
        if not vs["__VIEWSTATE"] or not vs["__EVENTVALIDATION"]:
            logger.error("NjBpuAdapter: viewstate missing from form page (prefix=%s)", prefix)
            return []

        payload = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            **vs,
            f"{_F}searchType": "Advanced",
            f"{_F}SearchText": "",
            f"{_F}AdvanceCaseNumber": prefix,
            f"{_F}AdvanceDocumentTitle": "",
            f"{_F}AdvancePartyName": "",
            f"{_F}AdvanceKeyword": "",
            f"{_F}OpenDateFrom": d_from,
            f"{_F}OpenDateTo": d_to,
            f"{_F}ListType": "Document",
            f"{_F}btnAdvanceSearch": "Search",
        }
        # The `Origin` header is REQUIRED — Incapsula 500s the POST without it.
        res = await client.post(
            _SEARCH_URL, data=payload, headers={"Origin": _BASE_URL, "Referer": _SEARCH_URL}
        )
        res.raise_for_status()
        if "Error.aspx" in str(res.url) or "cannot process this request" in res.text:
            logger.error(
                "NjBpuAdapter: origin Error.aspx for prefix=%s — Incapsula/Origin or viewstate "
                "drift (check the Origin header and __VIEWSTATE field names)",
                prefix,
            )
            return []
        return await self._collect_pages(client, res.text, prefix)

    async def _collect_pages(
        self, client: httpx.AsyncClient, first_html: str, label: str
    ) -> list[dict]:
        """Walk the gvSearchRs pager via __doPostBack until exhausted or capped."""
        total = parse_total(first_html)
        rows = parse_results(first_html)
        if not rows and total not in (0, None):
            logger.warning(
                "NjBpuAdapter: results table missing on page 1 (prefix=%s total=%s) — site drift?",
                label,
                total,
            )
            return []

        html = first_html
        page = 1
        while page < _MAX_PAGES and (total is None or len(rows) < total):
            target = next_page_target(html)
            if not target:
                break
            vs = extract_viewstate(html)
            await asyncio.sleep(_PAGE_DELAY)  # polite — dodge the Incapsula POST throttle
            r = await self._post_page(client, target, vs)
            if r is None:
                logger.warning(
                    "NjBpuAdapter: pagination failed prefix=%s at page %d — keeping %d of %d",
                    label,
                    page + 1,
                    len(rows),
                    total,
                )
                break
            page_rows = parse_results(r.text)
            if not page_rows:
                break
            rows.extend(page_rows)
            html = r.text
            page += 1

        if total is not None and len(rows) < total and page >= _MAX_PAGES:
            logger.warning(
                "NjBpuAdapter: prefix=%s hit page cap (%d) — collected %d of %d",
                label,
                _MAX_PAGES,
                len(rows),
                total,
            )
        return rows

    async def _post_page(
        self, client: httpx.AsyncClient, target: str, vs: dict[str, str]
    ) -> httpx.Response | None:
        """POST one pager step, retrying once — the datacenter-IP throttle hangs sporadically."""
        data = {"__EVENTTARGET": target, "__EVENTARGUMENT": "", **vs}
        headers = {"Origin": _BASE_URL, "Referer": _RESULTS_URL}
        for attempt in range(2):
            try:
                r = await client.post(_RESULTS_URL, data=data, headers=headers, timeout=30)
                r.raise_for_status()
                return r
            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                logger.warning("NjBpuAdapter: pager POST failed after retry (%s)", exc)
        return None

    def _finalize(self, rows: list[dict], d_from: str, d_to: str) -> list[RawFiling]:
        """Dedup by document_id; client electric check is a safety net over the prefix query."""
        seen: set[str] = set()
        electric: list[dict] = []
        dropped = 0
        for row in rows:
            if row["document_id"] in seen:
                continue
            seen.add(row["document_id"])
            if row["docket"] and not is_electric(row["docket"]):
                dropped += 1  # should be ~0 — server already scoped by prefix
                continue
            electric.append(row)

        filings = [_to_filing(r) for r in electric]

        # Anti-silent-zero floor: a normally-productive crawl returning zero is logged
        # loudly, never swallowed. (The dormant-IMM/PJM-calendar + this-backfill lesson.)
        if not filings:
            logger.warning(
                "NjBpuAdapter: ZERO electric filings for %s..%s (raw_rows=%d, dropped=%d) — "
                "verify the source is live and the parser still matches (gvSearchRs / "
                "DocumentHandler / Posted Date) and the prefix queries still return rows",
                d_from,
                d_to,
                len(rows),
                dropped,
            )
        else:
            logger.info(
                "NjBpuAdapter: %d electric filings (%d dropped, %d raw rows across %d prefixes) "
                "%s..%s",
                len(filings),
                dropped,
                len(rows),
                len(_QUERY_PREFIXES),
                d_from,
                d_to,
            )
        return filings
