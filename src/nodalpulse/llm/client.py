import logging

import anthropic
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from nodalpulse.settings import settings

logger = logging.getLogger(__name__)

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
async def classify(system: str, user: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = get_client()
    msg = await client.messages.create(
        model=model,
        max_tokens=512,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text  # type: ignore[return-value]


@_retry
async def extract(system: str, user: str, model: str = "claude-sonnet-4-6") -> str:
    client = get_client()
    msg = await client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if msg.stop_reason == "max_tokens":
        logger.warning("extract hit max_tokens at 8192 — retrying at 16384")
        msg = await client.messages.create(
            model=model,
            max_tokens=16384,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    return msg.content[0].text  # type: ignore[return-value]


@_retry
async def compose(system: str, user: str, model: str = "claude-sonnet-4-6") -> list[dict]:
    """Compose brief items. Returns list of {filing_id, summary, citation} dicts.

    Uses tool_choice to force structured output — Sonnet cannot return prose.
    Cache is on the stable system block; per-user user messages are never cached.
    """
    client = get_client()
    msg = await client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[_COMPOSE_TOOL],
        tool_choice={"type": "tool", "name": "emit_items"},
    )
    if msg.stop_reason == "max_tokens":
        logger.warning("compose hit max_tokens at 8192 — retrying at 16384")
        msg = await client.messages.create(
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
