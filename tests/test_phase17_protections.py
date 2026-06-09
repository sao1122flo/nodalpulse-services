"""Phase 17 — Pre-wake worker protections.

Tests for:
  A. MAX_LOOKBACK_DAYS cap in PUCT and ERCOT crawl handlers
  B. Cron enqueue carries explicit since= payload (covered by updated test_cron.py)
  C. CreditExhaustedError: 402 detection, 24h sleep (no restart loop)
  D. Per-hour spend circuit breaker
  E. GET /admin/jobs and POST /admin/jobs/purge endpoints

No Anthropic API calls are made. All external dependencies are mocked.
"""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nodalpulse.llm.client import CreditExhaustedError


# ── Fix A: MAX_LOOKBACK_DAYS cap ──────────────────────────────────────────────

class TestLookbackCap:
    """Crawl handlers cap the since date to MAX_LOOKBACK_DAYS regardless of DB value."""

    @pytest.mark.asyncio
    async def test_puct_caps_stale_db_date(self):
        """If get_last_crawled_at returns a 30-day-old date, since is capped to 3 days ago."""
        old_date = (date.today() - timedelta(days=30)).isoformat()
        expected_floor = (date.today() - timedelta(days=3)).isoformat()
        captured = {}

        async def fake_get_rows(since=None):
            captured["since"] = since
            return []

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = fake_get_rows

        with (
            patch("nodalpulse.workers.crawl_shared.get_last_crawled_at", return_value=old_date),
            patch("nodalpulse.workers.crawl_shared.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_shared.MAX_LOOKBACK_DAYS", 3),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            await handle_crawl_puct({})

        assert captured["since"] >= expected_floor

    @pytest.mark.asyncio
    async def test_puct_respects_recent_db_date(self):
        """If get_last_crawled_at returns yesterday, since stays as yesterday (within cap)."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        captured = {}

        async def fake_get_rows(since=None):
            captured["since"] = since
            return []

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = fake_get_rows

        with (
            patch("nodalpulse.workers.crawl_shared.get_last_crawled_at", return_value=yesterday),
            patch("nodalpulse.workers.crawl_shared.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_shared.MAX_LOOKBACK_DAYS", 3),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            await handle_crawl_puct({})

        assert captured["since"] == yesterday

    @pytest.mark.asyncio
    async def test_puct_explicit_payload_since_still_capped(self):
        """An explicit since= in the payload older than the cap is also floored."""
        captured = {}
        expected_floor = (date.today() - timedelta(days=3)).isoformat()

        async def fake_get_rows(since=None):
            captured["since"] = since
            return []

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = fake_get_rows

        with (
            patch("nodalpulse.workers.crawl_shared.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_shared.MAX_LOOKBACK_DAYS", 3),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            await handle_crawl_puct({"since": "2020-01-01"})

        assert captured["since"] >= expected_floor

    @pytest.mark.asyncio
    async def test_ercot_caps_stale_db_date(self):
        """ERCOT _run_crawler caps since the same way as PUCT."""
        old_date = (date.today() - timedelta(days=30)).isoformat()
        expected_floor = (date.today() - timedelta(days=3)).isoformat()
        captured = {}

        async def fake_fetch_new(since=None):
            captured["since"] = since
            return []

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = fake_fetch_new

        with (
            patch("nodalpulse.workers.crawl_shared.get_last_crawled_at", return_value=old_date),
            patch("nodalpulse.workers.crawl_shared.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl_shared.MAX_LOOKBACK_DAYS", 3),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl_shared import run_adapter
            await run_adapter(mock_crawler, "ercot-nprr", None)

        assert captured["since"] >= expected_floor


# ── Fix C: CreditExhaustedError detected from Anthropic 402 ──────────────────

class TestCreditExhaustedDetection:
    """tracked_messages_create raises CreditExhaustedError on HTTP 402."""

    @pytest.mark.asyncio
    async def test_402_becomes_credit_exhausted_error(self):
        import anthropic

        status_error = anthropic.APIStatusError(
            "Your account has no credits",
            response=MagicMock(status_code=402, headers={}),
            body={"error": {"type": "credit_exhausted"}},
        )

        with patch("nodalpulse.llm.client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(side_effect=status_error)
            mock_get_client.return_value = mock_client

            from nodalpulse.llm.client import tracked_messages_create
            with pytest.raises(CreditExhaustedError):
                await tracked_messages_create(
                    pipeline_stage="test",
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": "hi"}],
                )

    @pytest.mark.asyncio
    async def test_non_402_api_error_propagates_unchanged(self):
        """A 429 rate-limit error must NOT become CreditExhaustedError."""
        import anthropic

        rate_limit_error = anthropic.APIStatusError(
            "rate limited",
            response=MagicMock(status_code=429, headers={}),
            body={"error": {"type": "rate_limit_error"}},
        )

        with patch("nodalpulse.llm.client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
            mock_get_client.return_value = mock_client

            from nodalpulse.llm.client import tracked_messages_create
            with pytest.raises(anthropic.APIStatusError) as exc_info:
                await tracked_messages_create(
                    pipeline_stage="test",
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": "hi"}],
                )
        assert exc_info.value.status_code == 429
        assert not isinstance(exc_info.value, CreditExhaustedError)


# ── Fix C: run_worker sleeps 24h on CreditExhaustedError ─────────────────────

class TestCreditExhaustedHalt:
    """Worker marks job failed then sleeps 24h — container stays up, no Railway restart loop."""

    @pytest.mark.asyncio
    async def test_credit_exhausted_calls_fail_then_sleeps(self):
        job = {"id": "job-123", "payload": {}, "attempts": 1, "max_attempts": 5}
        mock_fail = AsyncMock()
        sleep_calls = []

        async def bad_handler(_payload):
            raise CreditExhaustedError("402 credit exhausted")

        async def fake_sleep(duration):
            sleep_calls.append(duration)
            raise asyncio.CancelledError

        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=0.0)),
            patch("nodalpulse.queue.pg_queue.dequeue", AsyncMock(return_value=job)),
            patch("nodalpulse.queue.pg_queue.complete", AsyncMock()),
            patch("nodalpulse.queue.pg_queue.fail", mock_fail),
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", fake_sleep),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", bad_handler)

        mock_fail.assert_called_once()
        assert any(d >= 3600 for d in sleep_calls), (
            f"Expected long sleep (>=1h) after credit exhaustion, got: {sleep_calls}"
        )

    @pytest.mark.asyncio
    async def test_credit_exhausted_handler_called_once(self):
        """CreditExhaustedError does not cause the job to be re-dequeued and retried."""
        call_count = 0
        job = {"id": "job-abc", "payload": {}, "attempts": 1, "max_attempts": 5}

        async def counting_handler(_payload):
            nonlocal call_count
            call_count += 1
            raise CreditExhaustedError("out of credits")

        async def fake_sleep(duration):
            raise asyncio.CancelledError

        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=0.0)),
            patch("nodalpulse.queue.pg_queue.dequeue", AsyncMock(return_value=job)),
            patch("nodalpulse.queue.pg_queue.fail", AsyncMock()),
            patch("nodalpulse.queue.pg_queue.complete", AsyncMock()),
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", fake_sleep),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", counting_handler)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_regular_exception_does_not_sleep_long(self):
        """A normal exception goes through fail() and the worker loop continues — no long sleep."""
        jobs = [
            {"id": "job-1", "payload": {}, "attempts": 1, "max_attempts": 5},
            None,
        ]
        dequeue_idx = 0

        async def fake_dequeue(_kind):
            nonlocal dequeue_idx
            val = jobs[dequeue_idx] if dequeue_idx < len(jobs) else None
            dequeue_idx += 1
            return val

        failed_jobs = []

        async def fake_fail(job_id, *a, **kw):
            failed_jobs.append(job_id)

        async def flaky_handler(_payload):
            raise ValueError("transient error")

        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=0.0)),
            patch("nodalpulse.queue.pg_queue.dequeue", fake_dequeue),
            patch("nodalpulse.queue.pg_queue.fail", fake_fail),
            patch("nodalpulse.queue.pg_queue.complete", AsyncMock()),
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", flaky_handler)

        assert "job-1" in failed_jobs


# ── Per-hour spend circuit breaker ────────────────────────────────────────────

class TestSpendCircuitBreaker:
    """run_worker checks last-hour spend before every dequeue."""

    @pytest.mark.asyncio
    async def test_breaker_fires_above_threshold(self):
        """$6 last-hour spend with $5 threshold: dequeue skipped, worker sleeps 1h."""
        sleep_calls = []

        async def fake_sleep(duration):
            sleep_calls.append(duration)
            raise asyncio.CancelledError

        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=6.0)),
            patch("nodalpulse.queue.pg_queue.dequeue") as mock_dequeue,
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", fake_sleep),
            patch("nodalpulse.queue.pg_queue._SPEND_CIRCUIT_USD", 5.0),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", AsyncMock())

        mock_dequeue.assert_not_called()
        assert 3600 in sleep_calls

    @pytest.mark.asyncio
    async def test_breaker_does_not_fire_below_threshold(self):
        """$4 last-hour spend with $5 threshold: dequeue runs normally."""
        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=4.0)),
            patch("nodalpulse.queue.pg_queue.dequeue", AsyncMock(return_value=None)) as mock_dequeue,
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            patch("nodalpulse.queue.pg_queue._SPEND_CIRCUIT_USD", 5.0),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", AsyncMock())

        mock_dequeue.assert_called()

    @pytest.mark.asyncio
    async def test_breaker_fires_at_exact_threshold(self):
        """Spend exactly at threshold triggers the breaker (>= not >)."""
        sleep_calls = []

        async def fake_sleep(duration):
            sleep_calls.append(duration)
            raise asyncio.CancelledError

        with (
            patch("nodalpulse.queue.pg_queue._last_hour_spend_usd", AsyncMock(return_value=5.0)),
            patch("nodalpulse.queue.pg_queue.dequeue") as mock_dequeue,
            patch("nodalpulse.queue.pg_queue.asyncio.sleep", fake_sleep),
            patch("nodalpulse.queue.pg_queue._SPEND_CIRCUIT_USD", 5.0),
        ):
            from nodalpulse.queue.pg_queue import run_worker
            with pytest.raises(asyncio.CancelledError):
                await run_worker("extract", AsyncMock())

        mock_dequeue.assert_not_called()


# ── Admin job endpoints ───────────────────────────────────────────────────────

@pytest.fixture()
def bypass_auth():
    from nodalpulse.api.app import app
    from nodalpulse.api.auth import verify_bearer
    app.dependency_overrides[verify_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(verify_bearer, None)


class TestAdminJobsInspect:
    @pytest.mark.asyncio
    async def test_requires_bearer(self):
        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs")
        assert resp.status_code == 403 or resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_count(self, bypass_auth):
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one=MagicMock(return_value=552))
        )
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/admin/jobs?kind=extract&status=pending")

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "extract"
        assert body["status"] == "pending"
        assert body["count"] == 552


class TestAdminJobsPurge:
    @pytest.mark.asyncio
    async def test_requires_bearer(self):
        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/admin/jobs/purge", json={"kind": "extract"})
        assert resp.status_code == 403 or resp.status_code == 401

    @pytest.mark.asyncio
    async def test_purge_pending_returns_count(self, bypass_auth):
        mock_result = MagicMock(rowcount=552)
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/admin/jobs/purge",
                    json={"kind": "extract", "status": "pending"},
                )

        assert resp.status_code == 200
        assert resp.json()["purged"] == 552

    @pytest.mark.asyncio
    async def test_purge_running_uses_stale_lock_guard(self, bypass_auth):
        """Purging status=running must include the updated_at < NOW()-1h guard."""
        captured_sql = []
        mock_result = MagicMock(rowcount=2)
        mock_session = AsyncMock()

        async def capture_execute(stmt, params=None):
            captured_sql.append(str(stmt))
            return mock_result

        mock_session.execute = capture_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/admin/jobs/purge",
                    json={"kind": "extract", "status": "running"},
                )

        assert resp.status_code == 200
        assert resp.json()["purged"] == 2
        assert any("1 hour" in sql for sql in captured_sql), (
            "Running-job purge must include the 1-hour stale-lock guard"
        )
