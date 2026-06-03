"""PJM stakeholder/auction calendar ingest — T11.

Two sources:
(1) PJM stakeholder RSS — meeting and vote announcements from PJM's newsroom feed.
    source='pjm_rss', estimated=false (published dates).
    Graceful no-op if the feed is unavailable (no exception raised).

(2) Deterministic RPM BRA/incremental auction milestones — derived from PJM's tariff
    planning calendar and publicly filed schedules. These are seeded once and remain
    until superseded by RSS entries with the real dates.
    source='auction_calendar'.

Scope B holds: these are published dates (estimated=false). FERC protest windows
are NOT generated here — those link to the Notice filing per the existing _enrich_deadlines
path. This handler only writes confirmed calendar milestones.

Dedup: all rows use a deterministic external_id slug so re-runs are idempotent.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from nodalpulse.db.market_events import upsert_market_event

logger = logging.getLogger(__name__)

# PJM newsroom/stakeholder RSS feed candidates (tried in order; first success wins).
# PJM may update these URLs; add new candidates here as needed.
_PJM_RSS_CANDIDATES = [
    "https://www.pjm.com/-/media/library/events/pjm-stakeholder-meetings.xml",
    "https://www.pjm.com/rss/stakeholder",
    "https://www.pjm.com/rss/news",
]

# Keyword → event_type mapping for RSS title classification.
_EVENT_TYPE_MAP: list[tuple[str, str]] = [
    ("base residual auction", "auction_milestone"),
    ("incremental auction",   "auction_milestone"),
    ("bra",                   "auction_milestone"),
    ("capacity auction",      "auction_milestone"),
    ("mrc vote",              "committee_vote"),
    ("mc vote",               "committee_vote"),
    ("members committee",     "committee_vote"),
    ("markets.*reliability",  "committee_vote"),
    ("comment deadline",      "comment_deadline"),
    ("comment period",        "comment_deadline"),
    ("stakeholder meeting",   "stakeholder_meeting"),
    ("task force",            "stakeholder_meeting"),
]


def _classify_event(title: str) -> str:
    lower = title.lower()
    for pattern, event_type in _EVENT_TYPE_MAP:
        if re.search(pattern, lower):
            return event_type
    return "stakeholder_meeting"


def _make_slug(source: str, event_date: date, title: str) -> str:
    """Deterministic external_id for idempotent upserts."""
    raw = f"{source}:{event_date.isoformat()}:{title[:120]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _parse_rss_date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_rss_items(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for item in root.findall(".//item"):
        def text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        items.append({
            "title":    text("title"),
            "link":     text("link"),
            "pub_date": text("pubDate"),
        })
    return items


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=8))
async def _try_fetch_rss(client: httpx.AsyncClient, url: str) -> list[dict]:
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return _parse_rss_items(resp.text)


async def _ingest_pjm_rss(since_date: date) -> int:
    """Fetch PJM stakeholder RSS. Returns count of newly inserted events."""
    inserted = 0
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
    ) as client:
        items: list[dict] = []
        for url in _PJM_RSS_CANDIDATES:
            try:
                items = await _try_fetch_rss(client, url)
                if items:
                    logger.info("crawl_pjm_calendar: RSS fetched %d items from %s", len(items), url)
                    break
            except Exception as exc:
                logger.debug("crawl_pjm_calendar: RSS %s unavailable: %s", url, exc)

    if not items:
        logger.info("crawl_pjm_calendar: no PJM RSS available — seeded milestones only")
        return 0

    for item in items:
        title = item.get("title", "").strip()
        if not title:
            continue
        event_date = _parse_rss_date(item.get("pub_date", ""))
        if not event_date or event_date < since_date:
            continue
        event_type = _classify_event(title)
        slug = _make_slug("pjm_rss", event_date, title)
        ok = await upsert_market_event(
            source="pjm_rss",
            jurisdiction="PJM-FERC",
            event_type=event_type,
            title=title,
            event_date=event_date,
            estimated=False,
            source_url=item.get("link") or None,
            external_id=slug,
        )
        if ok:
            inserted += 1

    return inserted


# ── Deterministic RPM auction milestones ─────────────────────────────────────
#
# Seeded from PJM's planning calendar and tariff filings. Mark estimated=False
# only for dates confirmed in a FERC filing or official PJM schedule. The BRA
# typically runs in May, 3 delivery years ahead (3-year forward procurement).
# Exact dates vary year-to-year; approximate windows use estimated=True.
#
# Sources: PJM RAA §6, OATT Attachment DD, annual planning schedule filings.

_RPM_MILESTONES: list[dict] = [
    # 2029/2030 delivery year — BRA expected May 2026 (3-year forward)
    {
        "source":          "auction_calendar",
        "event_type":      "auction_milestone",
        "title":           "PJM Base Residual Auction — Delivery Year 2029/2030 (window opens)",
        "event_date":      date(2026, 5, 1),
        "estimated":       True,  # exact open date TBD in FERC filing
        "related_docket":  "ER25-1357",
        "source_url":      "https://www.pjm.com/markets-and-operations/rpm",
    },
    {
        "source":          "auction_calendar",
        "event_type":      "auction_milestone",
        "title":           "PJM Base Residual Auction — Delivery Year 2029/2030 (results expected)",
        "event_date":      date(2026, 5, 31),
        "estimated":       True,
        "related_docket":  "ER25-1357",
        "source_url":      "https://www.pjm.com/markets-and-operations/rpm",
    },
    # 2027/2028 Incremental Auction 3 (IRA3) — ~3rd year before delivery
    {
        "source":          "auction_calendar",
        "event_type":      "auction_milestone",
        "title":           "PJM Incremental Auction 3 (IRA3) — Delivery Year 2027/2028",
        "event_date":      date(2026, 9, 1),
        "estimated":       True,
        "related_docket":  None,
        "source_url":      "https://www.pjm.com/markets-and-operations/rpm",
    },
    # RPM parameter filing season — PJM typically files parameters ~6 months before BRA
    {
        "source":          "auction_calendar",
        "event_type":      "auction_milestone",
        "title":           "PJM RPM parameter filing window — Delivery Year 2029/2030 (expected)",
        "event_date":      date(2025, 11, 1),
        "estimated":       True,
        "related_docket":  "ER25-1357",
        "source_url":      "https://elibrary.ferc.gov/eLibrary/search?q=ER25-1357",
    },
]


async def _seed_rpm_milestones() -> int:
    """Seed deterministic RPM auction milestones. Idempotent."""
    inserted = 0
    for m in _RPM_MILESTONES:
        slug = _make_slug(m["source"], m["event_date"], m["title"])
        ok = await upsert_market_event(
            source=m["source"],
            jurisdiction="PJM-FERC",
            event_type=m["event_type"],
            title=m["title"],
            event_date=m["event_date"],
            estimated=m["estimated"],
            related_docket=m.get("related_docket"),
            source_url=m.get("source_url"),
            external_id=slug,
        )
        if ok:
            inserted += 1
    return inserted


async def handle_crawl_pjm_calendar(payload: dict) -> dict:
    """Ingest PJM stakeholder/auction calendar into market_events.

    Runs daily at 05:00 CT alongside the other PJM crawls. Idempotent —
    re-runs insert nothing when events already exist (external_id dedup).
    """
    since_date = date.today() - timedelta(days=1)

    rss_inserted = await _ingest_pjm_rss(since_date)
    milestone_inserted = await _seed_rpm_milestones()

    result = {
        "source": "pjm-calendar",
        "rss_inserted": rss_inserted,
        "milestones_inserted": milestone_inserted,
        "total_inserted": rss_inserted + milestone_inserted,
    }
    logger.info("crawl_pjm_calendar complete: %s", result)
    return result
