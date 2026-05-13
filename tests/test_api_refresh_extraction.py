"""Tests for POST /extraction/refresh.

Mock targets are module-level names as imported into app.py:
  - nodalpulse.api.app.get_filing
  - nodalpulse.api.app.enqueue_idempotent

Auth bypass uses app.dependency_overrides (same pattern as test_api_recompose.py).
"""

import pytest
from httpx import ASGITransport, AsyncClient

import nodalpulse.api.auth as auth_mod
from nodalpulse.api.app import app
from nodalpulse.api.auth import verify_bearer

BASE = "http://test"
AUTH = {"Authorization": "Bearer test-key"}

_FILING = {"id": "00000000-0000-0000-0000-000000000010", "r2_key": "docs/test.pdf", "file_ext": "pdf", "doc_type": "puct-filing", "title": "Test filing"}


@pytest.fixture(autouse=False)
def bypass_auth():
    app.dependency_overrides[verify_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(verify_bearer, None)


@pytest.mark.asyncio
async def test_refresh_enqueues_new_job(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_filing", return_value=_FILING)
    mocker.patch("nodalpulse.api.app.enqueue_idempotent", return_value=("job-789", True))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/extraction/refresh",
            json={"filing_id": "00000000-0000-0000-0000-000000000010", "idempotency_key": "r1"},
            headers=AUTH,
        )

    assert resp.status_code == 201
    assert resp.json() == {"job_id": "job-789", "status": "queued"}


@pytest.mark.asyncio
async def test_refresh_idempotent_returns_existing(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_filing", return_value=_FILING)
    mocker.patch("nodalpulse.api.app.enqueue_idempotent", return_value=("job-789", False))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/extraction/refresh",
            json={"filing_id": "00000000-0000-0000-0000-000000000010", "idempotency_key": "r1"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-789", "status": "already_queued"}


@pytest.mark.asyncio
async def test_refresh_404_for_unknown_filing(mocker, bypass_auth):
    mocker.patch("nodalpulse.api.app.get_filing", return_value=None)
    enqueue_mock = mocker.patch("nodalpulse.api.app.enqueue_idempotent")

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/extraction/refresh",
            json={"filing_id": "00000000-0000-0000-0000-000000000099", "idempotency_key": "r2"},
            headers=AUTH,
        )

    assert resp.status_code == 404
    enqueue_mock.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_401_wrong_token(mocker):
    mocker.patch.object(auth_mod.settings, "services_api_key", "correct-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        resp = await client.post(
            "/extraction/refresh",
            json={"filing_id": "00000000-0000-0000-0000-000000000010", "idempotency_key": "r3"},
            headers={"Authorization": "Bearer wrong-key"},
        )

    assert resp.status_code == 401
