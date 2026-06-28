"""Brevo transactional email sender."""

import logging

import httpx

from nodalpulse.settings import settings

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


async def send_email(
    *,
    to_email: str,
    to_name: str | None = None,
    subject: str,
    html_content: str,
    text_content: str,
    unsubscribe_url: str,
) -> str | None:
    """Send a transactional email via Brevo.

    Returns the Brevo messageId on success, None on failure.
    List-Unsubscribe + List-Unsubscribe-Post headers are added for Gmail/Yahoo compliance.
    """
    if not settings.brevo_api_key:
        logger.warning("BREVO_API_KEY not set — skipping email to %s", to_email)
        return None

    payload = {
        "sender": {
            "name": settings.brevo_sender_name,
            "email": settings.brevo_sender_email,
        },
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_content,
        "textContent": text_content,
        # RFC 8058 one-click unsubscribe — required for Gmail/Yahoo bulk senders
        "headers": {
            "List-Unsubscribe": (
                f"<{unsubscribe_url}>, <mailto:unsubscribe@nodalpulse.com?subject=unsubscribe>"
            ),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _BREVO_URL,
            json=payload,
            headers={
                "api-key": settings.brevo_api_key,
                "Content-Type": "application/json",
            },
        )

    if resp.status_code not in (200, 201):
        logger.error("Brevo error %d for %s: %s", resp.status_code, to_email, resp.text[:300])
        return None

    return resp.json().get("messageId")
