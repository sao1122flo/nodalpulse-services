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
_MAX_PAGES = 60  # hard cap: 60×30 = 1800 docs per crawl tick

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
    """NJ BPU public-document firehose adapter (date-range, electric-scoped).

    Unlike CpucAdapter (per-proceeding watch set), this is a pure firehose: one
    Advanced search over the posted-date window returns every new document, which
    we then scope to electric dockets. No watch set needed.
    """

    source_slug = "njbpu"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = (
            datetime.fromisoformat(since).date() if since else date.today() - timedelta(days=1)
        )
        d_from = since_date.strftime("%m/%d/%Y")
        d_to = date.today().strftime("%m/%d/%Y")
        logger.info("NjBpuAdapter: firehose %s..%s", d_from, d_to)

        async with httpx.AsyncClient(
            base_url=_BASE_URL, follow_redirects=True, timeout=60, headers=_HEADERS
        ) as client:
            # Warm-up GET establishes Incapsula + session cookies (via Set-Cookie).
            try:
                await client.get(_SEARCH_URL)
                form = await client.get(_SEARCH_URL)
                form.raise_for_status()
            except Exception:
                logger.exception("NjBpuAdapter: GET Search.aspx failed — aborting tick")
                return []

            vs = extract_viewstate(form.text)
            if not vs["__VIEWSTATE"] or not vs["__EVENTVALIDATION"]:
                logger.error("NjBpuAdapter: viewstate missing from form page — site drift?")
                return []

            payload = {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                **vs,
                f"{_F}searchType": "Advanced",
                f"{_F}SearchText": "",
                f"{_F}AdvanceCaseNumber": "",
                f"{_F}AdvanceDocumentTitle": "",
                f"{_F}AdvancePartyName": "",
                f"{_F}AdvanceKeyword": "",
                f"{_F}OpenDateFrom": d_from,
                f"{_F}OpenDateTo": d_to,
                f"{_F}ListType": "Document",
                f"{_F}btnAdvanceSearch": "Search",
            }
            try:
                # The `Origin` header is REQUIRED — Incapsula 500s the POST without it.
                res = await client.post(
                    _SEARCH_URL,
                    data=payload,
                    headers={"Origin": _BASE_URL, "Referer": _SEARCH_URL},
                )
                res.raise_for_status()
            except Exception:
                logger.exception("NjBpuAdapter: search POST failed")
                return []

            if "Error.aspx" in str(res.url) or "cannot process this request" in res.text:
                logger.error(
                    "NjBpuAdapter: origin returned Error.aspx — Incapsula/Origin or viewstate "
                    "drift (check the Origin header and the __VIEWSTATE field names)"
                )
                return []

            all_rows = await self._collect_pages(client, res.text)

        return self._finalize(all_rows, since_date, d_from, d_to)

    async def _collect_pages(self, client: httpx.AsyncClient, first_html: str) -> list[dict]:
        """Walk the gvSearchRs pager via __doPostBack until exhausted or capped."""
        total = parse_total(first_html)
        rows = parse_results(first_html)
        if not rows and total != 0:
            # Table absent but the count marker didn't say "0" — structure drift.
            logger.warning(
                "NjBpuAdapter: results table not found on page 1 (total=%s) — possible site "
                "drift; treating as zero this tick",
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
            try:
                r = await client.post(
                    _RESULTS_URL,
                    data={"__EVENTTARGET": target, "__EVENTARGUMENT": "", **vs},
                    headers={"Origin": _BASE_URL, "Referer": _RESULTS_URL},
                )
                r.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "NjBpuAdapter: pagination POST failed on page %d (%s) — returning %d rows "
                    "collected so far",
                    page + 1,
                    exc,
                    len(rows),
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
                "NjBpuAdapter: hit page cap (%d) — collected %d of %d total",
                _MAX_PAGES,
                len(rows),
                total,
            )
        return rows

    def _finalize(
        self, rows: list[dict], since_date: date, d_from: str, d_to: str
    ) -> list[RawFiling]:
        """Dedup by document_id, scope to electric dockets, emit RawFilings."""
        seen: set[str] = set()
        electric: list[dict] = []
        dropped_non_electric = 0
        for row in rows:
            if row["document_id"] in seen:
                continue
            seen.add(row["document_id"])
            if row["docket"] and not is_electric(row["docket"]):
                dropped_non_electric += 1
                continue
            electric.append(row)

        filings = [_to_filing(r) for r in electric]

        # Anti-silent-zero floor: a normally-productive firehose that returns zero is
        # logged loudly, never swallowed. (The dormant-IMM/PJM-calendar lesson.)
        if not filings:
            logger.warning(
                "NjBpuAdapter: ZERO electric filings for %s..%s (raw_rows=%d, "
                "dropped_non_electric=%d) — verify the source is live and the parser still "
                "matches (gvSearchRs / DocumentHandler / Posted Date)",
                d_from,
                d_to,
                len(rows),
                dropped_non_electric,
            )
        else:
            logger.info(
                "NjBpuAdapter: %d electric filings (%d non-electric dropped, %d raw rows) %s..%s",
                len(filings),
                dropped_non_electric,
                len(rows),
                d_from,
                d_to,
            )
        return filings
