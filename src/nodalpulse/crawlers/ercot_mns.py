"""ERCOT Market Notices crawler — scrapes https://www.ercot.com/services/comm/mkt_notices.

Same Playwright-based approach as ercot_nprr.py since the site is Incapsula-protected.

Expected page structure:
  table > tr: [Date] [Market Notice ID] [Subject]

Each row links directly to a PDF or to a detail page that contains a PDF link.
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
LISTING_URL = f"{BASE_URL}/services/comm/mkt_notices"
_CHICAGO = ZoneInfo("America/Chicago")
_BROWSER_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class ErcotMarketNoticesCrawler(BaseCrawler):
    source_slug = "ercot-mn"

    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        since_date = date.fromisoformat(since) if since else date.today() - timedelta(days=2)
        logger.info("ERCOT Market Notices crawl since=%s", since_date)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=_UA)
            page = await ctx.new_page()

            rows = await _scrape_listing(page, since_date)
            logger.info("ERCOT MN: %d rows after date filter", len(rows))

            filings: list[RawFiling] = []
            for row in rows:
                try:
                    filing = await _fetch_notice_document(page, row)
                    if filing:
                        filings.append(filing)
                except Exception:
                    logger.exception("Error fetching MN %s", row.get("notice_id", "?"))

            await browser.close()

        logger.info("ERCOT MN: %d filings fetched", len(filings))
        return filings


async def _scrape_listing(page, since: date) -> list[dict]:
    try:
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
        # Wait for Incapsula challenge to complete and redirect to the real page
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # non-fatal; proceed to selector check
        await page.wait_for_selector("table tr", timeout=60_000)
    except Exception:
        logger.exception("ERCOT MN: failed to load listing page")
        try:
            logger.info("ERCOT MN: page url=%s content=%s", page.url, (await page.content())[:800])
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
                if (cells.length < 2) continue;
                const linkEl = tr.querySelector('a[href]');
                results.push({
                    date_raw: cells[0] ? cells[0].innerText.trim() : '',
                    notice_id: cells[1] ? cells[1].innerText.trim() : '',
                    subject: cells[2] ? cells[2].innerText.trim() : '',
                    href: linkEl ? linkEl.getAttribute('href') : null,
                });
            }
        }
        return results;
    }""")

    logger.info("ERCOT MN listing raw rows from page: %d", len(rows))

    filtered = []
    for row in rows:
        filed_at = _parse_date(row.get("date_raw", ""))
        if not filed_at:
            continue
        if date.fromisoformat(filed_at[:10]) < since:
            continue
        row["filed_at"] = filed_at
        filtered.append(row)
    logger.info("ERCOT MN: %d rows pass since=%s filter (total on page: %d)", len(filtered), since, len(rows))
    return filtered


async def _fetch_notice_document(page, row: dict) -> RawFiling | None:
    href = row.get("href")
    if not href:
        logger.warning("MN %s has no href", row.get("notice_id", "?"))
        return None

    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    # If href ends in .pdf, download directly; otherwise load detail page
    if url.lower().endswith(".pdf"):
        pdf_url = url
    else:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            pdf_url = await page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const pdf = links.find(a => a.href.toLowerCase().endsWith('.pdf'));
                return pdf ? pdf.href : null;
            }""")
        except Exception:
            logger.warning("MN %s: detail page failed %s", row.get("notice_id"), url)
            return None

    if not pdf_url:
        logger.warning("MN %s: no PDF found", row.get("notice_id", "?"))
        return None

    response = await page.request.get(pdf_url)
    if not response.ok:
        logger.warning("MN PDF download failed %s → %s", pdf_url, response.status)
        return None
    content = await response.body()

    notice_id = _clean_notice_id(row.get("notice_id", ""))
    subject = row.get("subject") or notice_id

    return RawFiling(
        source_slug="ercot-mn",
        external_id=notice_id,
        doc_type="ercot-mn",
        title=subject,
        source_url=url,
        filed_at=row["filed_at"],
        content=content,
        file_ext="pdf",
        metadata={
            "notice_id": notice_id,
            "date_raw": row.get("date_raw", ""),
            "pdf_url": pdf_url,
        },
    )


def _clean_notice_id(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip()) or raw[:32]


def _parse_date(raw: str) -> str | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            return naive.replace(tzinfo=_CHICAGO).astimezone(UTC).isoformat()
        except ValueError:
            continue
    return None
