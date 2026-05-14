import asyncio
import logging
import time
from decimal import Decimal

import anthropic
from sqlalchemy import text
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.settings import settings

logger = logging.getLogger(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _is_retryable(exc: BaseException) -> bool:
    """Retry on 529 (overloaded) and 5xx; fail loud on 4xx."""
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 529 or exc.status_code >= 500
    return isinstance(exc, anthropic.APIConnectionError)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(min=2, max=30),
    reraise=True,
)

# ── pricing ───────────────────────────────────────────────────────────────────

# Per-million-token prices fetched 2026-05-13 from https://www.anthropic.com/pricing
# Tuple order: (input, output, cache_write, cache_read)
PRICING_V1: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-sonnet-4-6": (
        Decimal("3.00"), Decimal("15.00"), Decimal("3.75"), Decimal("0.30"),
    ),
    "claude-haiku-4-5-20251001": (
        Decimal("1.00"), Decimal("5.00"), Decimal("1.25"), Decimal("0.10"),
    ),
}
PRICING_VERSION = "v1-2026-05-13"


def compute_cost(usage, model: str) -> Decimal:
    prices = PRICING_V1.get(model)
    if prices is None:
        return Decimal("0")
    p_in, p_out, p_cw, p_cr = prices
    mtok = Decimal("1000000")
    input_t = Decimal(usage.input_tokens)
    output_t = Decimal(usage.output_tokens)
    cache_w = Decimal(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_r = Decimal(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return (
        input_t / mtok * p_in
        + output_t / mtok * p_out
        + cache_w / mtok * p_cw
        + cache_r / mtok * p_cr
    ).quantize(Decimal("0.000001"))

# ── task strong-ref pool ──────────────────────────────────────────────────────

# asyncio.create_task() holds only a weak ref; GC can cancel in-flight tasks.
# This set keeps a strong ref until the done callback fires.
_pending_inserts: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    t = asyncio.create_task(coro)
    _pending_inserts.add(t)
    t.add_done_callback(_pending_inserts.discard)

# ── observability insert ──────────────────────────────────────────────────────

async def _persist_llm_call(
    *,
    response,
    error: str | None,
    latency_ms: int,
    pipeline_stage: str,
    model: str,
    filing_id,
    user_id,
    brief_id,
    prompt_version: str | None,
) -> None:
    try:
        if response is not None:
            u = response.usage
            input_tokens = u.input_tokens
            output_tokens = u.output_tokens
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            cache_creation = getattr(u, "cache_creation_input_tokens", 0) or 0
            cost = compute_cost(u, model)
            request_id = getattr(response, "id", None)
        else:
            input_tokens = output_tokens = cache_read = cache_creation = 0
            cost = Decimal("0")
            request_id = None

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO llm_calls (
                        model, pipeline_stage,
                        input_tokens, output_tokens,
                        cache_read_input_tokens, cache_creation_input_tokens,
                        cost_usd_estimate, pricing_version,
                        latency_ms, request_id, prompt_version, environment,
                        filing_id, user_id, brief_id, error
                    ) VALUES (
                        :model, :pipeline_stage,
                        :input_tokens, :output_tokens,
                        :cache_read_input_tokens, :cache_creation_input_tokens,
                        :cost_usd_estimate, :pricing_version,
                        :latency_ms, :request_id, :prompt_version, :environment,
                        CAST(:filing_id AS uuid), CAST(:user_id AS uuid),
                        CAST(:brief_id AS uuid), :error
                    )
                """),
                {
                    "model": model,
                    "pipeline_stage": pipeline_stage,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                    "cost_usd_estimate": str(cost),
                    "pricing_version": PRICING_VERSION,
                    "latency_ms": latency_ms,
                    "request_id": request_id,
                    "prompt_version": prompt_version,
                    "environment": settings.environment,
                    "filing_id": str(filing_id) if filing_id is not None else None,
                    "user_id": str(user_id) if user_id is not None else None,
                    "brief_id": str(brief_id) if brief_id is not None else None,
                    "error": error,
                },
            )
            await session.commit()
    except Exception as exc:
        logger.error("llm_calls insert failed: %s", exc)
        try:
            import sentry_sdk
            sentry_sdk.add_breadcrumb(
                message=f"llm_calls insert failed: {exc}", level="error", category="llm"
            )
        except Exception:
            pass

# ── tracked wrapper ───────────────────────────────────────────────────────────

async def tracked_messages_create(
    *,
    pipeline_stage: str,
    filing_id=None,
    user_id=None,
    brief_id=None,
    prompt_version: str | None = None,
    **anthropic_kwargs,
):
    """Drop-in for client.messages.create() that logs a row to llm_calls.

    The insert is fire-and-forget: a failed insert never raises to the caller.
    Returns the original anthropic.Message unchanged.
    """
    client = get_client()
    start = time.perf_counter()
    error: str | None = None
    response = None
    try:
        response = await client.messages.create(**anthropic_kwargs)
        return response
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        latency_ms = int((time.perf_counter() - start) * 1000)
        model = anthropic_kwargs.get("model", "unknown")
        _fire_and_forget(_persist_llm_call(
            response=response,
            error=error,
            latency_ms=latency_ms,
            pipeline_stage=pipeline_stage,
            model=model,
            filing_id=filing_id,
            user_id=user_id,
            brief_id=brief_id,
            prompt_version=prompt_version,
        ))

# ── tool schema for brief composition ────────────────────────────────────────

_COMPOSE_TOOL = {
    "name": "emit_items",
    "description": "Emit all composed brief items — one per input filing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "filing_id": {"type": "string"},
                        "summary": {"type": "string", "maxLength": 280},
                        "citation": {"type": "string"},
                    },
                    "required": ["filing_id", "summary", "citation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

# ── public API ────────────────────────────────────────────────────────────────

@_retry
async def classify(
    system: str,
    user: str,
    model: str = "claude-haiku-4-5-20251001",
    *,
    filing_id=None,
    prompt_version: str | None = None,
) -> str:
    # No cache_control here: claude-haiku-4-5 requires 4,096 tokens minimum to
    # engage prompt caching, which is economically unreachable for a triage prompt.
    msg = await tracked_messages_create(
        pipeline_stage="haiku-gate",
        filing_id=filing_id,
        prompt_version=prompt_version,
        model=model,
        max_tokens=512,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text  # type: ignore[return-value]


@_retry
async def extract(
    system: str,
    user: str,
    model: str = "claude-sonnet-4-6",
    *,
    filing_id=None,
    prompt_version: str | None = None,
) -> str:
    msg = await tracked_messages_create(
        pipeline_stage="sonnet-extract",
        filing_id=filing_id,
        prompt_version=prompt_version,
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if msg.stop_reason == "max_tokens":
        logger.warning("extract hit max_tokens at 8192 — retrying at 16384")
        msg = await tracked_messages_create(
            pipeline_stage="sonnet-extract",
            filing_id=filing_id,
            prompt_version=prompt_version,
            model=model,
            max_tokens=16384,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    return msg.content[0].text  # type: ignore[return-value]


@_retry
async def compose(
    system: str,
    user: str,
    model: str = "claude-sonnet-4-6",
    *,
    user_id=None,
    prompt_version: str | None = None,
) -> list[dict]:
    """Compose brief items. Returns list of {filing_id, summary, citation} dicts.

    Uses tool_choice to force structured output — Sonnet cannot return prose.
    Cache is on the stable system block; per-user user messages are never cached.
    """
    msg = await tracked_messages_create(
        pipeline_stage="brief-compose",
        user_id=user_id,
        prompt_version=prompt_version,
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[_COMPOSE_TOOL],
        tool_choice={"type": "tool", "name": "emit_items"},
    )
    if msg.stop_reason == "max_tokens":
        logger.warning("compose hit max_tokens at 8192 — retrying at 16384")
        msg = await tracked_messages_create(
            pipeline_stage="brief-compose",
            user_id=user_id,
            prompt_version=prompt_version,
            model=model,
            max_tokens=16384,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            tools=[_COMPOSE_TOOL],
            tool_choice={"type": "tool", "name": "emit_items"},
        )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "emit_items":
            return block.input.get("items", [])  # type: ignore[union-attr]
    logger.warning("compose: no emit_items block in response — returning empty list")
    return []
