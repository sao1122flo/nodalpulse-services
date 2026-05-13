"""Tests for POST /brief/recompose.

Mock targets are module-level names as imported into app.py:
  - nodalpulse.api.app.get_user_exists
  - nodalpulse.api.app.enqueue_idempotent

Auth bypass uses app.dependency_overrides — mocker.patch on the imported name
does not work because FastAPI's Depends() holds the original function object.
"""

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


@pytest.mark.asyncio
async def test_recompose_enqueues_new_job(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_user_exists", return_value=True)
    mocker.patch("nodalpulse.api.app.enqueue_idempotent", return_value=("job-123", True))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000001", "brief_date": "2026-05-12", "idempotency_key": "k1"},
            headers=AUTH,
        )

    assert resp.status_code == 201
    assert resp.json() == {"job_id": "job-123", "status": "queued"}


@pytest.mark.asyncio
async def test_recompose_idempotent_returns_existing(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_user_exists", return_value=True)
    mocker.patch("nodalpulse.api.app.enqueue_idempotent", return_value=("job-456", False))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000001", "brief_date": "2026-05-12", "idempotency_key": "k1"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-456", "status": "already_queued"}


@pytest.mark.asyncio
async def test_recompose_404_for_unknown_user(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_user_exists", return_value=False)
    enqueue_mock = mocker.patch("nodalpulse.api.app.enqueue_idempotent")

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000099", "brief_date": "2026-05-12", "idempotency_key": "k2"},
            headers=AUTH,
        )

    assert resp.status_code == 404
    enqueue_mock.assert_not_called()


@pytest.mark.asyncio
async def test_recompose_401_fail_closed_when_key_not_configured(mocker):
    # With services_api_key="" (default), verify_bearer rejects all requests immediately.
    mocker.patch.object(auth_mod.settings, "services_api_key", "")

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000001", "brief_date": "2026-05-12", "idempotency_key": "k3"},
            headers=AUTH,
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recompose_401_wrong_token(mocker):
    mocker.patch.object(auth_mod.settings, "services_api_key", "correct-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000001", "brief_date": "2026-05-12", "idempotency_key": "k4"},
            headers={"Authorization": "Bearer wrong-key"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recompose_422_missing_fields(mocker, bypass_auth):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/brief/recompose",
            json={"user_id": "00000000-0000-0000-0000-000000000001"},  # brief_date + idempotency_key missing
            headers=AUTH,
        )

    assert resp.status_code == 422
