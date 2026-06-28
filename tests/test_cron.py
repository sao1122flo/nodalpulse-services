"""Tests for the scheduler's persistent-state durability logic.

Clock control uses direct datetime injection (_tick / _startup_catchup accept
now_ct as a parameter) — no freezegun needed, no new dev dependencies.

Mock targets are module-level names as imported into nodalpulse.cron:
  nodalpulse.cron.is_crawl_done_for    (from db.scheduler)
  nodalpulse.cron.is_brief_done_for    (from db.scheduler)
  nodalpulse.cron.mark_crawl_done_for  (from db.scheduler)
  nodalpulse.cron.mark_brief_done_for  (from db.scheduler)
  nodalpulse.cron.enqueue              (from queue.pg_queue)
  nodalpulse.cron._enqueue_briefs_for_date  (local helper)

2026-05-12 is Tuesday; 2026-05-16 is Saturday.
"""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from nodalpulse.cron import _startup_catchup, _tick

_CHICAGO = ZoneInfo("America/Chicago")
_TODAY = date(2026, 5, 12)
_YESTERDAY = (_TODAY - timedelta(days=1)).isoformat()


# ── Startup catch-up tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_startup_catchup_both_missed(mocker):
    """Restart at 09:41 CT with empty DB → crawls and briefs both enqueued."""
    now_ct = datetime(2026, 5, 12, 9, 41, 0, tzinfo=_CHICAGO)

    mocker.patch("nodalpulse.cron.is_crawl_done_for", return_value=False)
    mocker.patch("nodalpulse.cron.is_brief_done_for", return_value=False)
    mock_mark_crawl = mocker.patch("nodalpulse.cron.mark_crawl_done_for")
    mock_mark_brief = mocker.patch("nodalpulse.cron.mark_brief_done_for")
    mock_enqueue = mocker.patch("nodalpulse.cron.enqueue")
    mock_enqueue_briefs = mocker.patch("nodalpulse.cron._enqueue_briefs_for_date", return_value=3)

    await _startup_catchup(now_ct)

    mock_enqueue.assert_any_call("crawl-puct", {"since": _YESTERDAY}, priority=10)
    mock_enqueue.assert_any_call("crawl-ercot", {"since": _YESTERDAY}, priority=10)
    mock_mark_crawl.assert_called_once_with(_TODAY)
    mock_enqueue_briefs.assert_called_once_with(_TODAY)
    mock_mark_brief.assert_called_once_with(_TODAY)


@pytest.mark.asyncio
async def test_startup_catchup_briefs_only(mocker):
    """Restart at 09:41 CT, crawl already done → only briefs enqueued."""
    now_ct = datetime(2026, 5, 12, 9, 41, 0, tzinfo=_CHICAGO)

    mocker.patch("nodalpulse.cron.is_crawl_done_for", return_value=True)
    mocker.patch("nodalpulse.cron.is_brief_done_for", return_value=False)
    mock_mark_crawl = mocker.patch("nodalpulse.cron.mark_crawl_done_for")
    mock_mark_brief = mocker.patch("nodalpulse.cron.mark_brief_done_for")
    mock_enqueue = mocker.patch("nodalpulse.cron.enqueue")
    mock_enqueue_briefs = mocker.patch("nodalpulse.cron._enqueue_briefs_for_date", return_value=3)

    await _startup_catchup(now_ct)

    mock_enqueue.assert_not_called()
    mock_mark_crawl.assert_not_called()
    mock_enqueue_briefs.assert_called_once_with(_TODAY)
    mock_mark_brief.assert_called_once_with(_TODAY)


# ── Normal tick tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_crawl_fires(mocker):
    """Tick at 05:02 CT with no prior run → crawl jobs enqueued and marked done."""
    now_ct = datetime(2026, 5, 12, 5, 2, 0, tzinfo=_CHICAGO)

    mocker.patch("nodalpulse.cron.is_crawl_done_for", return_value=False)
    mock_mark_crawl = mocker.patch("nodalpulse.cron.mark_crawl_done_for")
    mock_enqueue = mocker.patch("nodalpulse.cron.enqueue")
    # _tick gates the FERC/CAISO/PJM crawls on subscriber checks that hit the DB;
    # mock them so the test is hermetic (TX crawls fire regardless of subscribers).
    mocker.patch("nodalpulse.cron.market_has_subscribers", return_value=False)
    mocker.patch("nodalpulse.cron.enqueue_idempotent")

    await _tick(now_ct)

    mock_enqueue.assert_any_call("crawl-puct", {"since": _YESTERDAY}, priority=10)
    mock_enqueue.assert_any_call("crawl-ercot", {"since": _YESTERDAY}, priority=10)
    mock_mark_crawl.assert_called_once_with(_TODAY)


@pytest.mark.asyncio
async def test_tick_no_double_fire(mocker):
    """Tick at 05:02 CT when crawl already marked done → no enqueue, no DB write."""
    now_ct = datetime(2026, 5, 12, 5, 2, 0, tzinfo=_CHICAGO)

    mocker.patch("nodalpulse.cron.is_crawl_done_for", return_value=True)
    mock_mark_crawl = mocker.patch("nodalpulse.cron.mark_crawl_done_for")
    mock_enqueue = mocker.patch("nodalpulse.cron.enqueue")

    await _tick(now_ct)

    mock_enqueue.assert_not_called()
    mock_mark_crawl.assert_not_called()


@pytest.mark.asyncio
async def test_tick_weekend_skip(mocker):
    """Tick at 05:02 CT on Saturday → DB not queried, nothing enqueued."""
    # 2026-05-16 is Saturday (weekday() == 5)
    now_ct = datetime(2026, 5, 16, 5, 2, 0, tzinfo=_CHICAGO)

    mock_is_crawl = mocker.patch("nodalpulse.cron.is_crawl_done_for")
    mock_enqueue = mocker.patch("nodalpulse.cron.enqueue")

    await _tick(now_ct)

    mock_is_crawl.assert_not_called()
    mock_enqueue.assert_not_called()


# ── DB-layer idempotency test ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_crawl_done_upsert_is_idempotent(mocker):
    """Two calls to mark_crawl_done_for for the same date must not raise.

    Verifies the UPSERT (ON CONFLICT ... DO UPDATE) pattern in the SQL so that
    concurrent restarts cannot trample each other or produce a duplicate-key error.
    """
    mock_session = AsyncMock()
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("nodalpulse.db.scheduler.AsyncSessionLocal", return_value=mock_cm)

    from nodalpulse.db.scheduler import mark_crawl_done_for

    d = date(2026, 5, 12)
    await mark_crawl_done_for(d)
    await mark_crawl_done_for(d)

    assert mock_session.execute.call_count == 2
    sql = str(mock_session.execute.call_args_list[0][0][0])
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
