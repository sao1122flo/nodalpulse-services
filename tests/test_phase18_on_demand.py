"""Phase 18 — On-demand extraction mode tests.

Tests for:
  A. EXTRACTION_MODE gate in PUCT and ERCOT crawl handlers
  B. POST /extraction/refresh-docket — rate limiting, filing lookup, enqueue logic

No Anthropic API calls are made. All external dependencies are mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from nodalpulse.llm.client import CreditExhaustedError


# ── A: EXTRACTION_MODE gate ───────────────────────────────────────────────────

class TestExtractionModeGatePuct:
    """PUCT crawl handler only enqueues extract jobs in proactive mode."""

    @pytest.mark.asyncio
    async def test_on_demand_skips_extract_enqueue(self):
        """EXTRACTION_MODE=on-demand (default): saved filing does not produce extract job."""
        mock_filing = MagicMock()
        mock_filing.filed_at = "2026-05-12T00:00:00"
        mock_filing.file_ext = "pdf"
        mock_filing.doc_type = "order"
        mock_filing.external_id = "abc123"
        mock_filing.metadata = {"control_number": "59475"}

        async def fake_get_rows(since=None):
            return [{"external_id": "abc123"}]

        mock_crawler = MagicMock()
        mock_crawler.get_rows = fake_get_rows
        mock_crawler._download_filing = AsyncMock(return_value=mock_filing)

        captured_enqueues = []

        async def fake_enqueue(kind, payload, **kw):
            captured_enqueues.append(kind)
            return "job-id"

        with (
            patch("nodalpulse.workers.crawl.get_last_crawled_at", return_value="2026-05-11"),
            patch("nodalpulse.workers.crawl.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl.find_or_create_docket", AsyncMock(return_value="docket-uuid")),
            patch("nodalpulse.workers.crawl.upsert_filing", AsyncMock(return_value="filing-uuid")),
            patch("nodalpulse.workers.crawl.r2.upload"),
            patch("nodalpulse.workers.crawl.enqueue", fake_enqueue),
            patch("nodalpulse.workers.crawl.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            result = await handle_crawl_puct({})

        assert "extract" not in captured_enqueues, (
            "on-demand mode must not enqueue extract jobs"
        )
        assert result["saved"] == 1

    @pytest.mark.asyncio
    async def test_proactive_enqueues_extract(self):
        """EXTRACTION_MODE=proactive: saved filing enqueues an extract job."""
        mock_filing = MagicMock()
        mock_filing.filed_at = "2026-05-12T00:00:00"
        mock_filing.file_ext = "pdf"
        mock_filing.doc_type = "order"
        mock_filing.external_id = "abc123"
        mock_filing.metadata = {"control_number": "59475"}

        async def fake_get_rows(since=None):
            return [{"external_id": "abc123"}]

        mock_crawler = MagicMock()
        mock_crawler.get_rows = fake_get_rows
        mock_crawler._download_filing = AsyncMock(return_value=mock_filing)

        captured_enqueues = []

        async def fake_enqueue(kind, payload, **kw):
            captured_enqueues.append(kind)
            return "job-id"

        with (
            patch("nodalpulse.workers.crawl.get_last_crawled_at", return_value="2026-05-11"),
            patch("nodalpulse.workers.crawl.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl.find_or_create_docket", AsyncMock(return_value="docket-uuid")),
            patch("nodalpulse.workers.crawl.upsert_filing", AsyncMock(return_value="filing-uuid")),
            patch("nodalpulse.workers.crawl.r2.upload"),
            patch("nodalpulse.workers.crawl.enqueue", fake_enqueue),
            patch("nodalpulse.workers.crawl.EXTRACTION_MODE", "proactive"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            result = await handle_crawl_puct({})

        assert "extract" in captured_enqueues, (
            "proactive mode must enqueue an extract job for each saved filing"
        )
        assert result["saved"] == 1


class TestExtractionModeGateErcot:
    """ERCOT crawl handler only enqueues extract jobs in proactive mode."""

    @pytest.mark.asyncio
    async def test_on_demand_skips_extract_enqueue(self):
        """EXTRACTION_MODE=on-demand: ERCOT crawl does not enqueue extract jobs."""
        mock_filing = MagicMock()
        mock_filing.filed_at = "2026-05-12T00:00:00"
        mock_filing.file_ext = "pdf"
        mock_filing.doc_type = "nprr"
        mock_filing.external_id = "nprr999"
        mock_filing.content = b"data"

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = AsyncMock(return_value=[mock_filing])

        captured_enqueues = []

        async def fake_enqueue(kind, payload, **kw):
            captured_enqueues.append(kind)
            return "job-id"

        with (
            patch("nodalpulse.workers.crawl_ercot.get_last_crawled_at", return_value="2026-05-11"),
            patch("nodalpulse.workers.crawl_ercot.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl_ercot.ErcotNprrCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_ercot.ErcotMarketNoticesCrawler",
                  return_value=MagicMock(fetch_new=AsyncMock(return_value=[]))),
            patch("nodalpulse.workers.crawl_ercot.upsert_filing", AsyncMock(return_value="filing-uuid")),
            patch("nodalpulse.workers.crawl_ercot.r2.upload"),
            patch("nodalpulse.workers.crawl_ercot.enqueue", fake_enqueue),
            patch("nodalpulse.workers.crawl_ercot.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl_ercot import handle_crawl_ercot
            result = await handle_crawl_ercot({})

        assert "extract" not in captured_enqueues
        assert result["nprr"]["saved"] == 1

    @pytest.mark.asyncio
    async def test_proactive_enqueues_extract(self):
        """EXTRACTION_MODE=proactive: ERCOT crawl enqueues extract jobs."""
        mock_filing = MagicMock()
        mock_filing.filed_at = "2026-05-12T00:00:00"
        mock_filing.file_ext = "pdf"
        mock_filing.doc_type = "nprr"
        mock_filing.external_id = "nprr999"
        mock_filing.content = b"data"

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = AsyncMock(return_value=[mock_filing])

        captured_enqueues = []

        async def fake_enqueue(kind, payload, **kw):
            captured_enqueues.append(kind)
            return "job-id"

        with (
            patch("nodalpulse.workers.crawl_ercot.get_last_crawled_at", return_value="2026-05-11"),
            patch("nodalpulse.workers.crawl_ercot.get_source_id", return_value="src-uuid"),
            patch("nodalpulse.workers.crawl_ercot.ErcotNprrCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_ercot.ErcotMarketNoticesCrawler",
                  return_value=MagicMock(fetch_new=AsyncMock(return_value=[]))),
            patch("nodalpulse.workers.crawl_ercot.upsert_filing", AsyncMock(return_value="filing-uuid")),
            patch("nodalpulse.workers.crawl_ercot.r2.upload"),
            patch("nodalpulse.workers.crawl_ercot.enqueue", fake_enqueue),
            patch("nodalpulse.workers.crawl_ercot.EXTRACTION_MODE", "proactive"),
        ):
            from nodalpulse.workers.crawl_ercot import handle_crawl_ercot
            result = await handle_crawl_ercot({})

        assert "extract" in captured_enqueues
        assert result["nprr"]["saved"] == 1


# ── B: POST /extraction/refresh-docket ───────────────────────────────────────

@pytest.fixture()
def bypass_auth():
    from nodalpulse.api.app import app
    from nodalpulse.api.auth import verify_bearer
    app.dependency_overrides[verify_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(verify_bearer, None)


def _make_session_cm(execute_side_effects: list):
    """Build a mock AsyncSessionLocal context manager that returns execute calls in sequence."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute_side_effects)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


class TestRefreshDocketRequiresAuth:
    @pytest.mark.asyncio
    async def test_no_bearer_rejected(self):
        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/extraction/refresh-docket",
                json={"docket_number": "59475", "user_id": "user-uuid"},
            )
        assert resp.status_code in (401, 403)


class TestRefreshDocketRateLimit:
    @pytest.mark.asyncio
    async def test_returns_429_when_cap_exceeded(self, bypass_auth):
        """When the user has already hit the hourly cap, return 429."""
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=30))
        mock_cm = _make_session_cm([rate_mock])

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid"},
                )

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "rate_limit_exceeded"
        assert body["queued_last_hour"] == 30

    @pytest.mark.asyncio
    async def test_at_zero_usage_proceeds(self, bypass_auth):
        """User with no recent jobs is not rate-limited."""
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=0))
        filings_mock = MagicMock()
        filings_mock.mappings.return_value.all.return_value = []
        mock_cm = _make_session_cm([rate_mock, filings_mock])

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with (
            patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm),
            patch("nodalpulse.api.app.enqueue", AsyncMock(return_value="job-id")),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid"},
                )

        assert resp.status_code == 200


class TestRefreshDocketEnqueueLogic:
    @pytest.mark.asyncio
    async def test_enqueues_only_unextracted(self, bypass_auth):
        """Already-extracted filings are counted but not re-enqueued."""
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=0))

        rows = [
            {"filing_id": "f1", "r2_key": "raw/puct/f1.pdf", "doc_type": "order", "already_extracted": True},
            {"filing_id": "f2", "r2_key": "raw/puct/f2.pdf", "doc_type": "order", "already_extracted": False},
            {"filing_id": "f3", "r2_key": "raw/puct/f3.pdf", "doc_type": "order", "already_extracted": False},
        ]
        filings_mock = MagicMock()
        filings_mock.mappings.return_value.all.return_value = rows
        mock_cm = _make_session_cm([rate_mock, filings_mock])

        enqueued = []

        async def fake_enqueue(kind, payload, **kw):
            enqueued.append(payload["filing_id"])
            return "job-id"

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with (
            patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm),
            patch("nodalpulse.api.app.enqueue", fake_enqueue),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["already_extracted"] == 1
        assert body["queued"] == 2
        assert "f1" not in enqueued
        assert "f2" in enqueued
        assert "f3" in enqueued

    @pytest.mark.asyncio
    async def test_enqueue_payload_includes_user_id(self, bypass_auth):
        """Each enqueued job carries user_id for per-user rate-limit tracking."""
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=0))
        rows = [
            {"filing_id": "f1", "r2_key": "raw/puct/f1.pdf", "doc_type": "order", "already_extracted": False},
        ]
        filings_mock = MagicMock()
        filings_mock.mappings.return_value.all.return_value = rows
        mock_cm = _make_session_cm([rate_mock, filings_mock])

        enqueue_calls = []

        async def fake_enqueue(kind, payload, **kw):
            enqueue_calls.append((kind, payload))
            return "job-id"

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with (
            patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm),
            patch("nodalpulse.api.app.enqueue", fake_enqueue),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid-123"},
                )

        assert resp.status_code == 200
        assert len(enqueue_calls) == 1
        kind, payload = enqueue_calls[0]
        assert kind == "refresh-extraction"
        assert payload["filing_id"] == "f1"
        assert payload["user_id"] == "user-uuid-123"

    @pytest.mark.asyncio
    async def test_respects_effective_max_filings(self, bypass_auth):
        """Enqueue stops at the effective per-pin cap even if more un-extracted filings exist."""
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=0))
        # 10 un-extracted filings returned, but cap is 5
        rows = [
            {"filing_id": f"f{i}", "r2_key": f"raw/puct/f{i}.pdf",
             "doc_type": "order", "already_extracted": False}
            for i in range(10)
        ]
        filings_mock = MagicMock()
        filings_mock.mappings.return_value.all.return_value = rows
        mock_cm = _make_session_cm([rate_mock, filings_mock])

        enqueued = []

        async def fake_enqueue(kind, payload, **kw):
            enqueued.append(payload["filing_id"])
            return "job-id"

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with (
            patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm),
            patch("nodalpulse.api.app.enqueue", fake_enqueue),
            patch("nodalpulse.api.app._REFRESH_DOCKET_MAX_FILINGS", 5),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid"},
                )

        assert resp.status_code == 200
        assert resp.json()["queued"] == 5
        assert len(enqueued) == 5

    @pytest.mark.asyncio
    async def test_rate_limit_caps_effective_max(self, bypass_auth):
        """Remaining hourly quota limits how many jobs can be enqueued in a single call."""
        # User has already used 28 of 30 — only 2 slots left
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=28))
        rows = [
            {"filing_id": f"f{i}", "r2_key": f"raw/puct/f{i}.pdf",
             "doc_type": "order", "already_extracted": False}
            for i in range(5)
        ]
        filings_mock = MagicMock()
        filings_mock.mappings.return_value.all.return_value = rows
        mock_cm = _make_session_cm([rate_mock, filings_mock])

        enqueued = []

        async def fake_enqueue(kind, payload, **kw):
            enqueued.append(payload["filing_id"])
            return "job-id"

        from httpx import ASGITransport, AsyncClient
        from nodalpulse.api.app import app
        with (
            patch("nodalpulse.api.app.AsyncSessionLocal", return_value=mock_cm),
            patch("nodalpulse.api.app.enqueue", fake_enqueue),
            patch("nodalpulse.api.app._REFRESH_DOCKET_HOURLY_CAP", 30),
            patch("nodalpulse.api.app._REFRESH_DOCKET_MAX_FILINGS", 5),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/extraction/refresh-docket",
                    json={"docket_number": "59475", "user_id": "user-uuid"},
                )

        assert resp.status_code == 200
        assert resp.json()["queued"] == 2
        assert len(enqueued) == 2
