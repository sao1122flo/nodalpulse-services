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

from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date
from nodalpulse.db.scheduler import (
    is_brief_done_for,
    is_crawl_done_for,
    mark_brief_done_for,
    mark_crawl_done_for,
)
from nodalpulse.queue.pg_queue import enqueue

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
                await enqueue("crawl-puct",  {"since": since_date}, priority=10)
                await enqueue("crawl-ercot", {"since": since_date}, priority=10)
                await enqueue("crawl-ferc",  {"since": since_date}, priority=10)
                await enqueue("crawl-caiso", {"since": since_date}, priority=10)
                await enqueue("crawl-pjm",   {"since": since_date}, priority=10)
                await enqueue("crawl-imm",           {"since": since_date}, priority=10)
                await enqueue("crawl-pjm-calendar",  {}, priority=10)
                await mark_crawl_done_for(today)
                logger.info("Enqueued crawl-puct + crawl-ercot + crawl-ferc + crawl-caiso + crawl-pjm + crawl-imm + crawl-pjm-calendar for %s (since=%s)", today, since_date)
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
                await enqueue("crawl-ferc",  {"since": since_date}, priority=10)
                await enqueue("crawl-caiso", {"since": since_date}, priority=10)
                await enqueue("crawl-pjm",   {"since": since_date}, priority=10)
                await enqueue("crawl-imm",           {"since": since_date}, priority=10)
                await enqueue("crawl-pjm-calendar",  {}, priority=10)
                await mark_crawl_done_for(today)
                logger.info("Startup catch-up: enqueued crawls for %s (since=%s)", today, since_date)
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
