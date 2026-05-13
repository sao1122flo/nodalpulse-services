"""Tests for the LLM call tracking wrapper (src/nodalpulse/llm/client.py).

Follows the pytest-asyncio + mocker pattern from test_api_recompose.py.
No real DB or Anthropic credentials required — all external I/O is mocked.
"""

import asyncio
import gc
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nodalpulse.llm.client import (
    PRICING_VERSION,
    _fire_and_forget,
    _pending_inserts,
    _persist_llm_call,
    compute_cost,
    tracked_messages_create,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_usage(*, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_creation
    return u


def _make_response(usage=None, stop_reason="end_turn", request_id="msg_test"):
    r = MagicMock()
    r.usage = usage or _make_usage(input_tokens=10, output_tokens=5)
    r.stop_reason = stop_reason
    r.id = request_id
    r.content = []
    return r


@pytest.fixture
def mock_client(mocker):
    """Patch get_client() to return a mock whose messages.create is an AsyncMock."""
    mock_create = AsyncMock()
    mc = MagicMock()
    mc.messages.create = mock_create
    mocker.patch("nodalpulse.llm.client.get_client", return_value=mc)
    return mock_create


@pytest.fixture
def mock_session(mocker):
    """Patch AsyncSessionLocal to capture INSERT params without a real DB."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    mocker.patch("nodalpulse.llm.client.AsyncSessionLocal", return_value=cm)
    return session


# ── compute_cost ──────────────────────────────────────────────────────────────

def test_compute_cost_exact_sonnet():
    # Sonnet 4-6, 10k input fresh, 5k cache_read, 2k output
    # = 10000/1e6 * 3.00  +  5000/1e6 * 0.30  +  2000/1e6 * 15.00
    # =    0.030000        +     0.001500       +     0.030000
    # =    0.061500
    usage = _make_usage(input_tokens=10_000, output_tokens=2_000, cache_read=5_000)
    assert compute_cost(usage, "claude-sonnet-4-6") == Decimal("0.061500")


@pytest.mark.parametrize("model,inp,out,cr,cw,expected", [
    # haiku: 1M input only
    ("claude-haiku-4-5-20251001", 1_000_000, 0, 0, 0, Decimal("1.000000")),
    # sonnet: 1M cache_read only  (0.30/MTok)
    ("claude-sonnet-4-6", 0, 0, 1_000_000, 0, Decimal("0.300000")),
    # sonnet: 1M cache_write only (3.75/MTok)
    ("claude-sonnet-4-6", 0, 0, 0, 1_000_000, Decimal("3.750000")),
    # haiku: 1M output only (5.00/MTok)
    ("claude-haiku-4-5-20251001", 0, 1_000_000, 0, 0, Decimal("5.000000")),
    # unknown model → 0
    ("gpt-4-unknown", 1_000_000, 1_000_000, 0, 0, Decimal("0")),
    # zero tokens → 0
    ("claude-sonnet-4-6", 0, 0, 0, 0, Decimal("0.000000")),
])
def test_compute_cost_parametrized(model, inp, out, cr, cw, expected):
    usage = _make_usage(input_tokens=inp, output_tokens=out, cache_read=cr, cache_creation=cw)
    assert compute_cost(usage, model) == expected


def test_compute_cost_none_cache_fields():
    # SDK returns None for cache fields when caching isn't used — should not raise.
    usage = MagicMock()
    usage.input_tokens = 500
    usage.output_tokens = 100
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    # 500/1e6 * 3.00 + 100/1e6 * 15.00 = 0.001500 + 0.001500 = 0.003000
    assert compute_cost(usage, "claude-sonnet-4-6") == Decimal("0.003000")


# ── wrapper happy path ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wrapper_happy_path(mock_client, mock_session):
    usage = _make_usage(input_tokens=100, output_tokens=50)
    resp = _make_response(usage=usage, request_id="msg_abc")
    mock_client.return_value = resp

    result = await tracked_messages_create(
        pipeline_stage="haiku-gate",
        filing_id=None,
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": "hello"}],
    )

    # Response returned unchanged
    assert result is resp

    # Wait for the fire-and-forget insert to complete
    pending = list(_pending_inserts)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    mock_session.execute.assert_called_once()
    params = mock_session.execute.call_args[0][1]  # positional dict arg

    assert params["pipeline_stage"] == "haiku-gate"
    assert params["model"] == "claude-haiku-4-5-20251001"
    assert params["input_tokens"] == 100
    assert params["output_tokens"] == 50
    assert params["pricing_version"] == PRICING_VERSION
    assert params["request_id"] == "msg_abc"
    assert params["error"] is None
    # 100/1e6 * 1.00 + 50/1e6 * 5.00 = 0.000100 + 0.000250 = 0.000350
    assert Decimal(params["cost_usd_estimate"]) == Decimal("0.000350")


# ── wrapper failure path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wrapper_failure_path(mock_client, mock_session):
    mock_client.side_effect = RuntimeError("Anthropic down")

    with pytest.raises(RuntimeError, match="Anthropic down"):
        await tracked_messages_create(
            pipeline_stage="sonnet-extract",
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": "extract"}],
        )

    pending = list(_pending_inserts)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    mock_session.execute.assert_called_once()
    params = mock_session.execute.call_args[0][1]

    assert params["error"] is not None
    assert "RuntimeError" in params["error"]
    assert params["input_tokens"] == 0
    assert params["output_tokens"] == 0
    assert Decimal(params["cost_usd_estimate"]) == Decimal("0")


# ── insert failure is swallowed ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_failure_swallowed(mock_client, mocker):
    resp = _make_response()
    mock_client.return_value = resp

    # DB session raises on execute
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception("DB connection lost"))
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("nodalpulse.llm.client.AsyncSessionLocal", return_value=cm)

    # Should not raise despite DB failure
    result = await tracked_messages_create(
        pipeline_stage="brief-compose",
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": "compose"}],
    )
    assert result is resp

    pending = list(_pending_inserts)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # No exception leaked to caller — test passes by reaching here


# ── strong-ref pool: 100 concurrent inserts ───────────────────────────────────

@pytest.mark.asyncio
async def test_strong_refs_prevent_gc_loss():
    """All 100 fire-and-forget coroutines must complete even after a GC cycle."""
    completed: list[int] = []

    async def counter(i: int) -> None:
        completed.append(i)

    tasks_before = set(_pending_inserts)

    for i in range(100):
        _fire_and_forget(counter(i))

    our_tasks = [t for t in _pending_inserts if t not in tasks_before]
    assert len(our_tasks) == 100

    # Simulate GC pressure — without strong refs tasks would vanish here
    gc.collect()

    await asyncio.gather(*our_tasks, return_exceptions=True)

    assert len(completed) == 100
