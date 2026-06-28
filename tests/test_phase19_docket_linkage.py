"""Phase 19 — filings.docket_id population tests.

Tests for:
  A. find_or_create_docket — creates new row, idempotent on re-call
  B. upsert_filing with docket_id — new filing sets it, existing-null gets backfilled,
     existing-non-null is left alone
  C. handle_crawl_puct — wires find_or_create_docket and passes docket_id to upsert_filing
  D. POST /extraction/refresh-docket SQL — queries by docket_id join, not control_number
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ── A: find_or_create_docket ──────────────────────────────────────────────────

class TestFindOrCreateDocket:
    @pytest.mark.asyncio
    async def test_creates_new_docket_returns_uuid(self):
        """find_or_create_docket returns the UUID from the INSERT ... RETURNING clause."""
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one=MagicMock(return_value="docket-uuid-123"))
        )
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import find_or_create_docket
            result = await find_or_create_docket("src-uuid", "59475")

        assert result == "docket-uuid-123"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_sql_uses_on_conflict(self):
        """The INSERT must use ON CONFLICT so concurrent crawls don't race to failure."""
        captured_sql = []
        mock_session = AsyncMock()

        async def capture_execute(stmt, params=None):
            captured_sql.append(str(stmt))
            return MagicMock(scalar_one=MagicMock(return_value="docket-uuid"))

        mock_session.execute = capture_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import find_or_create_docket
            await find_or_create_docket("src-uuid", "59475")

        assert len(captured_sql) == 1
        assert "ON CONFLICT" in captured_sql[0]
        assert "DO UPDATE" in captured_sql[0]

    @pytest.mark.asyncio
    async def test_idempotent_two_calls(self):
        """Two calls with the same args both succeed without error."""
        call_count = 0

        async def fake_execute(stmt, params=None):
            nonlocal call_count
            call_count += 1
            return MagicMock(scalar_one=MagicMock(return_value="docket-uuid"))

        mock_session = AsyncMock()
        mock_session.execute = fake_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import find_or_create_docket
            r1 = await find_or_create_docket("src-uuid", "59475")
            r2 = await find_or_create_docket("src-uuid", "59475")

        assert r1 == r2 == "docket-uuid"
        assert call_count == 2


# ── B: upsert_filing with docket_id ──────────────────────────────────────────

class TestUpsertFilingDocketId:
    def _make_raw_filing(self):
        from nodalpulse.crawlers.base import RawFiling
        return RawFiling(
            source_slug="puct",
            external_id="59475_1_99999",
            doc_type="puct-order",
            title="Test Order",
            source_url="https://example.com",
            filed_at="2026-05-12T00:00:00",
            content=b"",
            file_ext="pdf",
            metadata={"control_number": "59475"},
        )

    @pytest.mark.asyncio
    async def test_new_filing_docket_id_included_in_insert(self):
        """New filing INSERT includes docket_id so the column is populated immediately."""
        captured_params = []

        async def capture_execute(stmt, params=None):
            captured_params.append(params or {})
            # First call = INSERT RETURNING (simulates new filing)
            return MagicMock(scalar_one_or_none=MagicMock(return_value="filing-uuid"))

        mock_session = AsyncMock()
        mock_session.execute = capture_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import upsert_filing
            result = await upsert_filing(self._make_raw_filing(), "src-uuid", "raw/key.pdf", docket_id="docket-uuid")

        assert result == "filing-uuid"
        assert captured_params[0]["docket_id"] == "docket-uuid"

    @pytest.mark.asyncio
    async def test_existing_filing_backfills_docket_id_when_null(self):
        """If the INSERT conflicts (existing filing), a UPDATE backfills docket_id IS NULL."""
        execute_results = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # INSERT conflict
            MagicMock(),  # UPDATE
        ]
        call_idx = 0

        async def fake_execute(stmt, params=None):
            nonlocal call_idx
            r = execute_results[call_idx]
            call_idx += 1
            return r

        mock_session = AsyncMock()
        mock_session.execute = fake_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import upsert_filing
            result = await upsert_filing(self._make_raw_filing(), "src-uuid", "raw/key.pdf", docket_id="docket-uuid")

        assert result is None  # existing filing — still skipped
        assert call_idx == 2  # INSERT + UPDATE both executed

    @pytest.mark.asyncio
    async def test_existing_filing_no_backfill_when_no_docket_id(self):
        """If no docket_id is passed and INSERT conflicts, no extra UPDATE is issued."""
        call_idx = 0

        async def fake_execute(stmt, params=None):
            nonlocal call_idx
            call_idx += 1
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session = AsyncMock()
        mock_session.execute = fake_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("nodalpulse.db.filings.AsyncSessionLocal", return_value=mock_cm):
            from nodalpulse.db.filings import upsert_filing
            result = await upsert_filing(self._make_raw_filing(), "src-uuid", "raw/key.pdf")

        assert result is None
        assert call_idx == 1  # only the INSERT, no UPDATE


# ── C: handle_crawl_puct wires docket linkage ────────────────────────────────

class TestCrawlPuctDocketWiring:
    @pytest.mark.asyncio
    async def test_find_or_create_docket_called_with_control_number(self):
        """Crawl handler extracts control_number from metadata and calls find_or_create_docket."""
        from nodalpulse.crawlers.base import RawFiling

        mock_filing = RawFiling(
            source_slug="puct",
            external_id="59475_1_99999",
            doc_type="puct-order",
            title="Test — 59475",
            source_url="https://interchange.puc.texas.gov/Documents/59475_1_99999.PDF",
            filed_at="2026-05-12T00:00:00+00:00",
            content=b"",  # deferred
            file_ext="pdf",
            metadata={"control_number": "59475", "item_number": "1", "item_key": "59475_1",
                      "item_type": "", "item_type_raw": "", "description_raw": "", "party": ""},
        )

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = AsyncMock(return_value=[mock_filing])

        mock_find = AsyncMock(return_value="docket-uuid-59475")
        mock_upsert = AsyncMock(return_value="filing-uuid")

        with (
            patch("nodalpulse.workers.crawl_shared.get_last_crawled_at", AsyncMock(return_value="2026-05-11")),
            patch("nodalpulse.workers.crawl_shared.get_source_id", AsyncMock(return_value="src-uuid")),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_shared.find_or_create_docket", mock_find),
            patch("nodalpulse.workers.crawl_shared.upsert_filing", mock_upsert),
            patch("nodalpulse.workers.crawl_shared.upsert_filing_dockets", AsyncMock()),
            patch("nodalpulse.workers.crawl_shared.get_all_tracked_docket_ids", AsyncMock(return_value=set())),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            await handle_crawl_puct({})

        mock_find.assert_called_once_with("src-uuid", "59475", jurisdiction="PUCT", title="Test — 59475")
        mock_upsert.assert_called_once()
        assert mock_upsert.call_args.kwargs.get("docket_id") == "docket-uuid-59475"

    @pytest.mark.asyncio
    async def test_no_control_number_skips_docket_lookup(self):
        """Filing without control_number in metadata does not call find_or_create_docket."""
        from nodalpulse.crawlers.base import RawFiling

        mock_filing = RawFiling(
            source_slug="puct",
            external_id="no_cn_filing",
            doc_type="puct-order",
            title="No CN Filing",
            source_url="https://interchange.puc.texas.gov/Documents/no_cn_filing.PDF",
            filed_at="2026-05-12T00:00:00+00:00",
            content=b"",  # deferred
            file_ext="pdf",
            metadata={},  # no control_number
        )

        mock_crawler = MagicMock()
        mock_crawler.fetch_new = AsyncMock(return_value=[mock_filing])

        mock_find = AsyncMock(return_value="docket-uuid")

        with (
            patch("nodalpulse.workers.crawl_shared.get_last_crawled_at", AsyncMock(return_value="2026-05-11")),
            patch("nodalpulse.workers.crawl_shared.get_source_id", AsyncMock(return_value="src-uuid")),
            patch("nodalpulse.workers.crawl.PuctCrawler", return_value=mock_crawler),
            patch("nodalpulse.workers.crawl_shared.find_or_create_docket", mock_find),
            patch("nodalpulse.workers.crawl_shared.upsert_filing", AsyncMock(return_value="filing-uuid")),
            patch("nodalpulse.workers.crawl_shared.upsert_filing_dockets", AsyncMock()),
            patch("nodalpulse.workers.crawl_shared.get_all_tracked_docket_ids", AsyncMock(return_value=set())),
            patch("nodalpulse.workers.crawl_shared.EXTRACTION_MODE", "on-demand"),
        ):
            from nodalpulse.workers.crawl import handle_crawl_puct
            await handle_crawl_puct({})

        mock_find.assert_not_called()


# ── D: Phase 18 endpoint uses docket_id join ─────────────────────────────────

@pytest.fixture()
def bypass_auth():
    from nodalpulse.api.app import app
    from nodalpulse.api.auth import verify_bearer
    app.dependency_overrides[verify_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(verify_bearer, None)


class TestRefreshDocketUsesdocketIdJoin:
    @pytest.mark.asyncio
    async def test_sql_joins_on_docket_id_not_control_number(self, bypass_auth):
        """The filing lookup SQL must reference dockets table + docket_id, not control_number."""
        captured_sql = []
        rate_mock = MagicMock(scalar_one=MagicMock(return_value=0))

        async def capture_execute(stmt, params=None):
            captured_sql.append(str(stmt))
            if len(captured_sql) == 1:
                return rate_mock
            result = MagicMock()
            result.mappings.return_value.all.return_value = []
            return result

        mock_session = AsyncMock()
        mock_session.execute = capture_execute
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

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
        filing_sql = captured_sql[1]  # second execute = the filing lookup
        assert "docket_id" in filing_sql, "SQL must filter by docket_id"
        assert "control_number" not in filing_sql, "Legacy control_number match must be removed"
        assert "dockets" in filing_sql, "SQL must join through dockets table"
