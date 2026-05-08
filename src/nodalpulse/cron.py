"""Weekday brief scheduler — long-lived process that enqueues compose-brief jobs.

Runs as a separate Railway service (scheduler process in Procfile).

DST safety: uses ZoneInfo("America/Chicago") so the 06:00 window is always
correct local CT time regardless of standard/daylight transitions.
"""

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from nodalpulse.db.briefs import get_active_user_ids, get_already_enqueued_for_date
from nodalpulse.queue.pg_queue import enqueue

logger = logging.getLogger(__name__)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")

_CHICAGO = ZoneInfo("America/Chicago")
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


async def run_scheduler() -> None:
    logger.info("Cron scheduler starting — brief window: weekdays %02d:00–%02d:%02d CT",
                _BRIEF_HOUR, _BRIEF_HOUR, _BRIEF_WINDOW_MIN)
    triggered: set[date] = set()

    while True:
        await asyncio.sleep(60)

        now_ct = datetime.now(_CHICAGO)

        # Weekdays only (0=Monday … 4=Friday)
        if now_ct.weekday() >= 5:
            continue

        # Fire once during 06:00–06:04 CT
        if now_ct.hour != _BRIEF_HOUR or now_ct.minute >= _BRIEF_WINDOW_MIN:
            continue

        today = now_ct.date()
        if today in triggered:
            continue

        logger.info("Enqueuing daily briefs for %s", today)
        try:
            count = await _enqueue_briefs_for_date(today)
            triggered.add(today)
            logger.info("Enqueued %d compose-brief jobs for %s", count, today)
        except Exception:
            logger.exception("Failed to enqueue briefs for %s — will retry next minute", today)


if __name__ == "__main__":
    asyncio.run(run_scheduler())
