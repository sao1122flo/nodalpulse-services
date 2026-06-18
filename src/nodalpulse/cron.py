"""Weekday brief scheduler — long-lived process that enqueues compose-brief jobs.

Runs as a separate Railway service (scheduler process in Procfile).

DST safety: uses ZoneInfo("America/Chicago") so the 06:00 window is always
correct local CT time regardless of standard/daylight transitions.

Restart durability: daily run state is persisted in scheduler_daily_runs.
On startup, _startup_catchup() checks whether today's windows were missed
and enqueues catch-up jobs before entering the main loop.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date, market_has_subscribers
from nodalpulse.db.scheduler import (
    is_brief_done_for,
    is_crawl_done_for,
    mark_brief_done_for,
    mark_crawl_done_for,
)
from nodalpulse.queue.pg_queue import enqueue, enqueue_idempotent
from nodalpulse.workers.salience import _iso_week_start

logger = logging.getLogger(__name__)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=__import__("sys").stdout)

_CHICAGO = ZoneInfo("America/Chicago")
_CRAWL_HOUR = 5         # 05:xx CT — crawl runs 1 hour before briefs
_BRIEF_HOUR = 6         # 06:xx CT
_BRIEF_WINDOW_MIN = 5   # fire once during the first 5 minutes of the hour


async def _enqueue_briefs_for_date(brief_date: date) -> int:
    user_ids = await get_active_user_ids()
    already = await get_already_enqueued_for_date(brief_date)
    count = 0
    for uid in user_ids:
        if uid not in already:
            await enqueue(
                "compose-brief",
                {"user_id": uid, "brief_date": brief_date.isoformat()},
                priority=5,
            )
            count += 1
    return count


async def _tick(now_ct: datetime) -> None:
    """Process one scheduler tick for the given wall-clock time.

    Extracted from the loop so tests can call it directly with a frozen clock,
    without needing to mock asyncio.sleep or control the event loop.
    """
    if now_ct.weekday() >= 5:
        return

    today = now_ct.date()

    # Crawl at 05:00–05:04 CT
    if now_ct.hour == _CRAWL_HOUR and now_ct.minute < _BRIEF_WINDOW_MIN:
        if not await is_crawl_done_for(today):
            try:
                since_date = (today - timedelta(days=1)).isoformat()

                # Texas markets: always crawl (all paid tiers include TX).
                await enqueue("crawl-puct",  {"since": since_date}, priority=10)
                await enqueue("crawl-ercot", {"since": since_date}, priority=10)

                caiso_active = await market_has_subscribers("CAISO")
                pjm_active   = await market_has_subscribers("PJM")

                # crawl-ferc serves CAISO-FERC *and* PJM-FERC (ER/EL dockets).
                # Must run if EITHER market has subscribers — gating on CAISO alone
                # would starve PJM-FERC dockets and produce empty briefs for PJM users.
                # crawl-ferc-discovery is the broad metadata sweep for entity-match (#85);
                # gated the same way since it reads from the same FERC Electric library.
                if caiso_active or pjm_active:
                    await enqueue("crawl-ferc",           {"since": since_date}, priority=10)
                    await enqueue("crawl-ferc-discovery", {"since": since_date}, priority=9)
                else:
                    logger.info("No CAISO/PJM subscribers — skipping crawl-ferc/discovery for %s", today)

                if caiso_active:
                    await enqueue("crawl-caiso", {"since": since_date}, priority=10)
                    await enqueue("crawl-cpuc",  {"since": since_date}, priority=10)
                else:
                    logger.info("No CAISO subscribers — skipping crawl-caiso/cpuc for %s", today)

                if pjm_active:
                    await enqueue("crawl-pjm",          {"since": since_date}, priority=10)
                    await enqueue("crawl-imm",          {"since": since_date}, priority=10)
                    await enqueue("crawl-pjm-calendar", {}, priority=10)
                else:
                    logger.info("No PJM subscribers — skipping crawl-pjm/imm/pjm-calendar for %s", today)

                await mark_crawl_done_for(today)

                # Salience ranking — enqueue once per market per day (idempotent).
                # Runs after crawls complete so today's filings are in the DB.
                week_start_str = _iso_week_start(today).isoformat()
                today_str = today.isoformat()
                for sal_market in ["PUCT", "ERCOT"]:
                    await enqueue_idempotent(
                        "compute-market-salience",
                        {"market": sal_market, "week_start": week_start_str},
                        idempotency_key=f"salience-{sal_market}-{today_str}",
                        priority=8,
                    )
                if caiso_active or pjm_active:
                    await enqueue_idempotent(
                        "compute-market-salience",
                        {"market": "FERC", "week_start": week_start_str},
                        idempotency_key=f"salience-FERC-{today_str}",
                        priority=8,
                    )
                if caiso_active:
                    await enqueue_idempotent(
                        "compute-market-salience",
                        {"market": "CAISO", "week_start": week_start_str},
                        idempotency_key=f"salience-CAISO-{today_str}",
                        priority=8,
                    )
                if pjm_active:
                    await enqueue_idempotent(
                        "compute-market-salience",
                        {"market": "PJM", "week_start": week_start_str},
                        idempotency_key=f"salience-PJM-{today_str}",
                        priority=8,
                    )

                logger.info(
                    "Enqueued crawls for %s (caiso=%s, pjm=%s, since=%s)",
                    today, caiso_active, pjm_active, since_date,
                )
            except Exception:
                logger.exception("Failed to enqueue crawls for %s — will retry next minute", today)

    # Brief at 06:00–06:04 CT
    if now_ct.hour == _BRIEF_HOUR and now_ct.minute < _BRIEF_WINDOW_MIN:
        if not await is_brief_done_for(today):
            try:
                count = await _enqueue_briefs_for_date(today)
                await mark_brief_done_for(today)
                logger.info("Enqueued %d compose-brief jobs for %s", count, today)
            except Exception:
                logger.exception("Failed to enqueue briefs for %s — will retry next minute", today)


async def _startup_catchup(now_ct: datetime) -> None:
    """Enqueue any windows that were missed before this process started.

    Uses >= comparison so any restart after the window opens (05:xx, 06:xx)
    triggers a catch-up. The DB mark prevents double-firing if the main loop
    later reaches the same window on the same date.
    """
    if now_ct.weekday() >= 5:
        return

    today = now_ct.date()

    if now_ct.hour >= _CRAWL_HOUR:
        if not await is_crawl_done_for(today):
            try:
                since_date = (today - timedelta(days=1)).isoformat()
                await enqueue("crawl-puct",  {"since": since_date}, priority=10)
                await enqueue("crawl-ercot", {"since": since_date}, priority=10)
                caiso_active = await market_has_subscribers("CAISO")
                pjm_active   = await market_has_subscribers("PJM")
                if caiso_active or pjm_active:
                    await enqueue("crawl-ferc",           {"since": since_date}, priority=10)
                    await enqueue("crawl-ferc-discovery", {"since": since_date}, priority=9)
                if caiso_active:
                    await enqueue("crawl-caiso", {"since": since_date}, priority=10)
                    await enqueue("crawl-cpuc",  {"since": since_date}, priority=10)
                if pjm_active:
                    await enqueue("crawl-pjm",          {"since": since_date}, priority=10)
                    await enqueue("crawl-imm",          {"since": since_date}, priority=10)
                    await enqueue("crawl-pjm-calendar", {}, priority=10)
                await mark_crawl_done_for(today)
                logger.info(
                    "Startup catch-up: enqueued crawls for %s (caiso=%s, pjm=%s, since=%s)",
                    today, caiso_active, pjm_active, since_date,
                )
            except Exception:
                logger.exception("Startup catch-up: failed to enqueue crawls for %s", today)

    if now_ct.hour >= _BRIEF_HOUR:
        if not await is_brief_done_for(today):
            try:
                count = await _enqueue_briefs_for_date(today)
                await mark_brief_done_for(today)
                logger.info("Startup catch-up: enqueued %d briefs for %s", count, today)
            except Exception:
                logger.exception("Startup catch-up: failed to enqueue briefs for %s", today)


async def run_scheduler() -> None:
    logger.info(
        "Cron scheduler starting — crawl: weekdays %02d:00 CT, briefs: weekdays %02d:00–%02d:%02d CT",
        _CRAWL_HOUR, _BRIEF_HOUR, _BRIEF_HOUR, _BRIEF_WINDOW_MIN,
    )
    await _startup_catchup(datetime.now(_CHICAGO))

    while True:
        await asyncio.sleep(60)
        await _tick(datetime.now(_CHICAGO))


if __name__ == "__main__":
    asyncio.run(run_scheduler())
