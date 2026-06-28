"""CAISO regulatory filings index adapter.

Fetches the static HTML index at caiso.com/legal-regulatory/regulatory-filings-orders/filings,
scopes to the FERC section (first div.table-responsive on the page), and returns one
RawFiling per row whose posted date is >= since.

Page structure (as of 2026-06):
  - Static server-rendered HTML; all rows present in initial response (~159 rows spanning 2017+)
  - <div class="table-responsive"> is the FERC section (first on page)
  - Each <tr> has:
      td.doc-table-title → <a href="/documents/<slug>.pdf" data-track-file-name="<slug>">
      td[data-mobile-title="Type"] → "Filing", "Motion", "Answer", …
      td.doc-lib-date[data-sort="<unix_ts>"] → Unix timestamp (UTC)
  - Docket numbers appear in the title text: (ER25-2442) or (ER23-2309, ER24-1394, and EL26-34)

R2 strategy: content=b"" — source_url stored at crawl time; PDF uploaded to R2 at extraction.
"""

import logging
import re
from datetime import UTC, date, datetime

import httpx
from selectolax.parser import HTMLParser, Node
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.crawlers.base import MarketAdapter, RawFiling

logger = logging.getLogger(__name__)

_INDEX_URL = "https://www.caiso.com/legal-regulatory/regulatory-filings-orders/filings"
_BASE_URL = "https://www.caiso.com"

# FERC docket regex — same pattern as FercAdapter (T2).
_DOCKET_RE = re.compile(r"\b([A-Z]{1,4}\d{2}-\d+(?:-\d{3})?)\b")
_SUB_DOCKET_RE = re.compile(r"-\d{3}$")

# Ordered longest-first so "compliance filing" matches before "filing".
_TITLE_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("tariff amendment", "tariff_amendment"),
    ("compliance filing", "compliance_filing"),
    ("informational filing", "informational_filing"),
    ("joint motion", "motion"),
    ("motion for", "motion"),
    ("motion to", "motion"),
    ("petition for", "petition"),
    ("petition to", "petition"),
    ("notice of", "notice"),
    ("answer to", "answer"),
    ("answer of", "answer"),
    ("comment on", "comment"),
    ("comments on", "comment"),
    ("errata", "errata"),
    ("order", "order"),
    ("report", "report"),
    ("motion", "motion"),
    ("answer", "answer"),
    ("comment", "comment"),
]


def _normalize_docket(d: str) -> str:
    return _SUB_DOCKET_RE.sub("", d).upper()


def _parse_dockets(text: str) -> list[str]:
    """Return ordered list of unique normalized FERC docket IDs found in text."""
    seen: dict[str, None] = {}
    for m in _DOCKET_RE.finditer(text.upper()):
        n = _normalize_docket(m.group(1))
        if n not in seen:
            seen[n] = None
    return list(seen)


def _infer_doc_type(title: str, raw_type: str) -> str:
    title_lower = title.lower()
    for pattern, doc_type in _TITLE_TYPE_PATTERNS:
        if pattern in title_lower:
            return doc_type
    # Fall back to Type column if title didn't match anything specific
    raw_lower = raw_type.lower()
    if "report" in raw_lower:
        return "report"
    if "order" in raw_lower:
        return "order"
    return "filing"


class CaisoAdapter(MarketAdapter):
    """Crawls the CAISO regulatory filings index (FERC section) for new filings.

    Uses selectolax for HTML parsing (already in dependencies). All PDFs have
    content=b"" — source_url is preserved; R2 upload deferred to extraction time.
    """

    source_slug = "caiso"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = date.fromisoformat(since) if since else date.today()
        html = await self._fetch_index()
        filings = self._parse_ferc_filings(html, since_date)
        logger.info("CaisoAdapter: since=%s returning %d filings", since_date, len(filings))
        return filings

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _fetch_index(self) -> str:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
        ) as client:
            resp = await client.get(_INDEX_URL)
            resp.raise_for_status()
            return resp.text

    def _parse_ferc_filings(self, html: str, since_date: date) -> list[RawFiling]:
        # Anchor to the FERC section by id — not positionally (sections could reorder).
        # Slice the HTML between id="ferc" and id="cpuc" so the parser only sees
        # the FERC table; div.table-responsive within that slice is unambiguously FERC.
        # Use 'id="ferc">' (with closing >) to skip the nav-link that also contains
        # data-track-id="ferc" — that substring matches 'id="ferc"' but is not the section heading.
        ferc_start = html.find('id="ferc">')
        if ferc_start == -1:
            logger.warning('CaisoAdapter: id="ferc"> anchor not found — page structure changed?')
            return []
        cpuc_start = html.find('id="cpuc">', ferc_start)
        ferc_html = html[ferc_start:cpuc_start] if cpuc_start != -1 else html[ferc_start:]

        tree = HTMLParser(ferc_html)
        ferc_container = tree.css_first("div.table-responsive")
        if not ferc_container:
            logger.warning("CaisoAdapter: FERC section table not found — page structure changed?")
            return []

        filings: list[RawFiling] = []
        skipped_date = 0
        dropped_no_docket: list[str] = []

        for tr in ferc_container.css("tr"):
            result = self._parse_row(tr, since_date)
            if result is None:
                skipped_date += 1
            elif result is False:
                link = tr.css_first("td.doc-table-title a")
                title_snippet = (link.text(strip=True) if link else "?")[:80]
                dropped_no_docket.append(title_snippet)
            else:
                filings.append(result)

        # Always log drop count so T6 can confirm we're shedding only CPUC/CEC/Court
        # rows, not real FERC filings whose docket didn't match _DOCKET_RE.
        if dropped_no_docket:
            logger.info(
                "CaisoAdapter: dropped %d rows with no FERC docket (expected: CPUC/CEC/Court) "
                "— sample: %s",
                len(dropped_no_docket),
                dropped_no_docket[:3],
            )
        logger.info(
            "CaisoAdapter: since=%s kept=%d dropped_no_docket=%d skipped_old=%d",
            since_date,
            len(filings),
            len(dropped_no_docket),
            skipped_date,
        )
        return filings

    def _parse_row(self, tr: Node, since_date: date) -> "RawFiling | None | bool":
        """Parse one <tr> row.
        Returns RawFiling on success, None if row is too old, False if no dockets found.
        """
        title_td = tr.css_first("td.doc-table-title")
        type_td = tr.css_first("td[data-mobile-title=Type]")
        date_td = tr.css_first("td.doc-lib-date")

        if not (title_td and type_td and date_td):
            return None

        link = title_td.css_first("a")
        if not link:
            return None

        raw_sort = date_td.attributes.get("data-sort", "0")
        data_sort = int(raw_sort) if raw_sort.isdigit() else 0
        if not data_sort:
            return None

        filed_dt = datetime.fromtimestamp(data_sort, tz=UTC)
        if filed_dt.date() < since_date:
            return None  # too old

        href = link.attributes.get("href", "")
        external_id = link.attributes.get("data-track-file-name", "")
        title_text = link.text(strip=True)
        raw_type = type_td.text(strip=True)

        if not external_id or not title_text:
            return None

        docket_numbers = _parse_dockets(title_text)
        if not docket_numbers:
            return False  # CPUC/CEC/Court rows — no FERC docket IDs

        source_url = (_BASE_URL + href) if href.startswith("/") else href
        doc_type = _infer_doc_type(title_text, raw_type)

        return RawFiling(
            source_slug="caiso",
            external_id=external_id,
            doc_type=doc_type,
            title=title_text,
            source_url=source_url,
            filed_at=filed_dt.isoformat(),
            content=b"",
            file_ext="pdf",
            metadata={
                "docket_numbers": docket_numbers,
                "filing_type": raw_type,
            },
        )
