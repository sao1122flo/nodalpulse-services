"""Tests for GET /admin/llm-costs.

Mirrors the auth + response-shape pattern from test_api_recompose.py.
DB is mocked; the SQL aggregation itself is verified by the prod spot-check.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import nodalpulse.api.auth as auth_mod
from nodalpulse.api.app import app
from nodalpulse.api.auth import verify_bearer

BASE = "http://test"
AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture(autouse=False)
def bypass_auth():
    app.dependency_overrides[verify_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(verify_bearer, None)


def _make_session_mock(by_day_rows, totals_row):
    """Build an AsyncSessionLocal mock that returns controlled aggregation rows."""
    by_day_result = MagicMock()
    by_day_result.mappings.return_value.all.return_value = by_day_rows

    totals_result = MagicMock()
    totals_result.mappings.return_value.first.return_value = totals_row

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[by_day_result, totals_result])

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ── auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_costs_401_no_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.get("/admin/llm-costs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_llm_costs_401_wrong_token(mocker):
    mocker.patch.object(auth_mod.settings, "services_api_key", "correct-key")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.get("/admin/llm-costs", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


# ── aggregation shape ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_costs_200_aggregation(mocker, bypass_auth):
    # Fixture: 10 simulated llm_calls across 2 stages on 2026-05-13
    by_day_rows = [
        {
            "day": date(2026, 5, 13),
            "stage": "haiku-gate",
            "model": "claude-haiku-4-5-20251001",
            "pricing_version": "v1-2026-05-13",
            "calls": 7,
            "input_tokens": 70_000,
            "output_tokens": 3_500,
            "cache_read_input_tokens": 42_000,
            "cost_usd": Decimal("0.075950"),
        },
        {
            "day": date(2026, 5, 13),
            "stage": "sonnet-extract",
            "model": "claude-sonnet-4-6",
            "pricing_version": "v1-2026-05-13",
            "calls": 3,
            "input_tokens": 30_000,
            "output_tokens": 6_000,
            "cache_read_input_tokens": 18_000,
            "cost_usd": Decimal("0.100400"),
        },
    ]
    totals_row = {"calls": 10, "cost_usd": Decimal("0.176350")}

    mocker.patch("nodalpulse.api.app.AsyncSessionLocal", return_value=_make_session_mock(by_day_rows, totals_row))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.get("/admin/llm-costs?days=1")

    assert resp.status_code == 200
    body = resp.json()

    # Range shape
    assert "from" in body["range"]
    assert "to" in body["range"]
    assert body["range"]["to"] == date.today().isoformat()

    # Totals
    assert body["totals"]["calls"] == 10
    assert abs(body["totals"]["cost_usd"] - 0.176350) < 1e-6

    # by_day entries
    assert len(body["by_day"]) == 2

    haiku = next(r for r in body["by_day"] if r["stage"] == "haiku-gate")
    assert haiku["model"] == "claude-haiku-4-5-20251001"
    assert haiku["pricing_version"] == "v1-2026-05-13"
    assert haiku["calls"] == 7
    assert haiku["input_tokens"] == 70_000
    assert haiku["cache_read_input_tokens"] == 42_000
    assert abs(haiku["cost_usd"] - 0.075950) < 1e-6

    sonnet = next(r for r in body["by_day"] if r["stage"] == "sonnet-extract")
    assert sonnet["calls"] == 3
    assert abs(sonnet["cost_usd"] - 0.100400) < 1e-6


@pytest.mark.asyncio
async def test_llm_costs_empty_table(mocker, bypass_auth):
    """Empty llm_calls table returns zeros, not an error."""
    mocker.patch(
        "nodalpulse.api.app.AsyncSessionLocal",
        return_value=_make_session_mock([], {"calls": 0, "cost_usd": Decimal("0")}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.get("/admin/llm-costs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["calls"] == 0
    assert body["totals"]["cost_usd"] == 0.0
    assert body["by_day"] == []


@pytest.mark.asyncio
async def test_llm_costs_days_clamp(mocker, bypass_auth):
    """days param is clamped to 1–90; values outside that range don't error."""
    # Each call to AsyncSessionLocal() must return a *fresh* mock — the two
    # requests would exhaust a single side_effect list.
    def fresh_cm():
        return _make_session_mock([], {"calls": 0, "cost_usd": Decimal("0")})

    mocker.patch("nodalpulse.api.app.AsyncSessionLocal", side_effect=fresh_cm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp_low = await client.get("/admin/llm-costs?days=0")
        resp_high = await client.get("/admin/llm-costs?days=999")

    assert resp_low.status_code == 200
    assert resp_high.status_code == 200
