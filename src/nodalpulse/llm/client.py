import anthropic

from nodalpulse.settings import settings

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def classify(system: str, user: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = get_client()
    msg = await client.messages.create(
        model=model,
        max_tokens=512,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text  # type: ignore[return-value]


async def extract(system: str, user: str, model: str = "claude-sonnet-4-6") -> str:
    client = get_client()
    msg = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text  # type: ignore[return-value]
