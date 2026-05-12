"""ERCOT NPRR crawler — scrapes https://www.ercot.com/mktrules/issues/reports/nprr/pending.

ERCOT's site is protected by Incapsula WAF; httpx alone cannot pass the JS challenge.
Playwright renders the page fully, bypasses the challenge, and extracts table rows.

Document structure on the listing page (9 columns):
  [#] [Title] [Description] [Date Posted] [Sponsor] [Urgent] [Protocol Sections] [Current Status] [Effective Date(s)]

Each # links to a detail page that contains the downloadable protocol document.
We capture the first PDF on the detail page as the canonical document.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from nodalpulse.crawlers.base import BaseCrawler, RawFiling

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ercot.com"
LISTING_URL = f"{BASE_URL}/mktrules/issues/reports/nprr/pending"
_CHICAGO = ZoneInfo("America/Chicago")
_BROWSER_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# Doc-type map keyed on status / title keywords (lowercase)
_DOC_TYPE_MAP = {
    "market notice": "ercot-mn",
    "nprr": "ercot-nprr",
    "pgrr": "ercot-nprr",
    "mprr": "ercot-nprr",
    "nogrr": "ercot-nprr",
    "smogrr": "ercot-nprr",
    "rmgrr": "ercot-nprr",
    "scr": "ercot-nprr",
}


class ErcotNprrCrawler(BaseCrawler):
    source_slug = "ercot-nprr"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = date.fromisoformat(since) if since else date.today() - timedelta(days=2)
        logger.info("ERCOT NPRR crawl since=%s", since_date)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=_UA)
            page = await ctx.new_page()

            rows = await _scrape_listing(page, since_date)
            logger.info("ERCOT NPRR: %d rows after date filter", len(rows))

            filings: list[RawFiling] = []
            for row in rows:
                try:
                    filing = await _fetch_nprr_document(page, row)
                    if filing:
                        filings.append(filing)
                except Exception:
                    logger.exception("Error fetching NPRR %s", row.get("nprr_number", "?"))

            await browser.close()

        logger.info("ERCOT NPRR: %d filings fetched", len(filings))
        return filings


async def _scrape_listing(page, since: date) -> list[dict]:
    """Load the NPRR listing and extract rows since `since`. Reuses caller's page."""
    try:
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
        # Wait for Incapsula challenge to complete and redirect to the real page
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # non-fatal; proceed to selector check
        await page.wait_for_selector("table tr", timeout=60_000)
    except Exception:
        logger.exception("ERCOT NPRR: failed to load listing page")
        try:
            logger.info("ERCOT NPRR: page url=%s content=%s", page.url, (await page.content())[:800])
        except Exception:
            pass
        return []

    rows = await page.evaluate("""() => {
        const results = [];
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            const trs = table.querySelectorAll('tr');
            for (const tr of trs) {
                const cells = tr.querySelectorAll('td');
                if (cells.length < 3) continue;
                const linkEl = cells[0].querySelector('a');
                results.push({
                    nprr_number: cells[0].innerText.trim(),
                    title: cells[1] ? cells[1].innerText.trim() : '',
                    date_posted: cells[3] ? cells[3].innerText.trim() : '',
                    status: cells[7] ? cells[7].innerText.trim() : '',
                    effective_date: cells[8] ? cells[8].innerText.trim() : '',
                    detail_href: linkEl ? linkEl.getAttribute('href') : null,
                });
            }
        }
        return results;
    }""")

    logger.info("ERCOT NPRR listing raw rows from page: %d", len(rows))

    filtered = []
    for row in rows:
        filed_at = _parse_date(row.get("date_posted", ""))
        if not filed_at:
            continue
        filed_date = date.fromisoformat(filed_at[:10])
        if filed_date < since:
            continue
        row["filed_at"] = filed_at
        filtered.append(row)
    logger.info("ERCOT NPRR: %d rows pass since=%s filter (total on page: %d)", len(filtered), since, len(rows))
    return filtered


async def _fetch_nprr_document(page, row: dict) -> RawFiling | None:
    """Navigate to an NPRR detail page and download the first PDF document."""
    detail_href = row.get("detail_href")
    if not detail_href:
        logger.warning("NPRR %s has no detail href", row.get("nprr_number"))
        return None

    detail_url = detail_href if detail_href.startswith("http") else f"{BASE_URL}{detail_href}"
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("a[href]", timeout=10_000)
    except Exception:
        logger.warning("NPRR detail page timeout for %s", detail_url)
        return None

    # Find the first PDF link on the detail page
    pdf_url: str | None = await page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('a[href]'));
        const pdf = links.find(a => a.href.toLowerCase().endsWith('.pdf'));
        return pdf ? pdf.href : null;
    }""")

    if not pdf_url:
        logger.warning("NPRR %s: no PDF found at %s", row.get("nprr_number"), detail_url)
        return None

    response = await page.request.get(pdf_url)
    if not response.ok:
        logger.warning("NPRR PDF download failed %s → %s", pdf_url, response.status)
        return None
    content = await response.body()

    nprr_num = _clean_nprr_number(row.get("nprr_number", ""))
    doc_type = _doc_type_from_number(nprr_num)
    title = row.get("title") or nprr_num

    return RawFiling(
        source_slug="ercot-nprr",
        external_id=nprr_num,
        doc_type=doc_type,
        title=title,
        source_url=detail_url,
        filed_at=row["filed_at"],
        content=content,
        file_ext="pdf",
        metadata={
            "nprr_number": nprr_num,
            "date_posted": row.get("date_posted", ""),
            "effective_date": row.get("effective_date", ""),
            "status": row.get("status", ""),
            "pdf_url": pdf_url,
        },
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean_nprr_number(raw: str) -> str:
    """'NPRR 1287' → 'NPRR1287', 'nprr1287' → 'NPRR1287'."""
    cleaned = re.sub(r"\s+", "", raw.upper())
    return cleaned or raw[:32]


def _doc_type_from_number(nprr_number: str) -> str:
    for prefix in ("NPRR", "PGRR", "MPRR", "NOGRR", "SMOGRR", "RMGRR", "SCR"):
        if nprr_number.upper().startswith(prefix):
            return "ercot-nprr"
    return "ercot-nprr"


def _parse_date(raw: str) -> str | None:
    """Parse ERCOT date strings → UTC ISO-8601. Accepts M/D/YYYY and YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            return naive.replace(tzinfo=_CHICAGO).astimezone(UTC).isoformat()
        except ValueError:
            continue
    return None
