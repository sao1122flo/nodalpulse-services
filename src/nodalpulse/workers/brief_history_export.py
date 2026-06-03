"""Worker handler: brief_history_export

Generates a zip archive of a user's full brief history (HTML files + manifest),
uploads it to R2, produces a presigned download URL, and emails the link.

Job payload: { "user_id": "<uuid>", "user_email": "<str>" }
"""

import io
import json
import logging
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.email.brevo import send_email
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

_PRESIGNED_EXPIRY_SECONDS = 60 * 60 * 24  # 24 hours


async def _fetch_briefs(user_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    id::text,
                    date::text,
                    html_r2_key,
                    txt_r2_key,
                    citation_count,
                    created_at
                FROM briefs
                WHERE user_id = CAST(:uid AS uuid)
                  AND send_status = 'sent'
                ORDER BY date DESC
            """),
            {"uid": user_id},
        )
        return [dict(r) for r in result.mappings().all()]


def _build_zip(briefs: list[dict]) -> bytes:
    """Fetch HTML for each brief from R2 and pack into a zip archive."""
    buf = io.BytesIO()
    manifest = []

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for brief in briefs:
            date_str = brief["date"]
            html_key = brief.get("html_r2_key")

            entry = {
                "id": brief["id"],
                "date": date_str,
                "citation_count": brief["citation_count"],
                "file": f"briefs/{date_str}.html" if html_key else None,
            }

            if html_key and r2.exists(html_key):
                try:
                    html_bytes = r2.download(html_key)
                    zf.writestr(f"briefs/{date_str}.html", html_bytes)
                except Exception as exc:
                    logger.warning("Failed to download brief %s from R2: %s", brief["id"], exc)
                    entry["file"] = None

            manifest.append(entry)

        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))

    return buf.getvalue()


async def handle_brief_history_export(payload: dict) -> dict:
    user_id = payload.get("user_id", "")
    user_email = payload.get("user_email", "")

    if not user_id or not user_email:
        raise ValueError("brief_history_export requires user_id and user_email in payload")

    logger.info("brief_history_export start user=%s", user_id)

    briefs = await _fetch_briefs(user_id)
    if not briefs:
        logger.info("brief_history_export no briefs for user=%s", user_id)
        # Still send an email so the user knows the export completed.
        _send_empty_email(user_email)
        return {"user_id": user_id, "brief_count": 0, "status": "empty"}

    zip_bytes = _build_zip(briefs)
    zip_key = f"exports/{user_id}/brief-history-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.zip"
    r2.upload(zip_key, zip_bytes, "application/zip")

    presigned_url = r2.get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": __import__("nodalpulse.settings", fromlist=["settings"]).settings.r2_bucket, "Key": zip_key},
        ExpiresIn=_PRESIGNED_EXPIRY_SECONDS,
    )

    _send_ready_email(user_email, presigned_url, len(briefs))

    logger.info(
        "brief_history_export done user=%s briefs=%d zip_bytes=%d",
        user_id, len(briefs), len(zip_bytes),
    )
    return {"user_id": user_id, "brief_count": len(briefs), "status": "ok", "zip_key": zip_key}


def _send_ready_email(to: str, download_url: str, brief_count: int) -> None:
    expiry = (datetime.now(UTC) + timedelta(seconds=_PRESIGNED_EXPIRY_SECONDS)).strftime(
        "%B %d, %Y at %H:%M UTC"
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation">
<tr><td align="center" style="padding:48px 16px">
<table width="100%" style="max-width:480px;background:#1a1d27;border:1px solid #2a2d3a;border-radius:8px;padding:40px 36px" cellpadding="0" cellspacing="0" role="presentation">
<tr><td>
<p style="margin:0 0 6px;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#6b7280">NodalPulse</p>
<h1 style="margin:0 0 14px;font-size:20px;font-weight:600;color:#f3f4f6;letter-spacing:-.02em">Your brief history is ready</h1>
<p style="margin:0 0 6px;font-size:14px;color:#9ca3af;line-height:1.65">Your archive of {brief_count} brief{"s" if brief_count != 1 else ""} is ready to download.</p>
<p style="margin:0 0 28px;font-size:14px;color:#9ca3af;line-height:1.65">The link expires on {expiry}.</p>
<a href="{download_url}" style="display:inline-block;background:#4f46e5;color:#fff;font-size:14px;font-weight:500;padding:11px 22px;border-radius:6px;text-decoration:none">Download archive</a>
<p style="margin:28px 0 0;font-size:12px;color:#4b5563">The archive includes HTML files for each brief and a manifest.json index.</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""

    try:
        send_email(
            to_email=to,
            subject="Your NodalPulse brief history is ready",
            html_body=html,
        )
    except Exception as exc:
        logger.error("Failed to send brief history export email to %s: %s", to, exc)


def _send_empty_email(to: str) -> None:
    try:
        send_email(
            to_email=to,
            subject="NodalPulse brief history export",
            html_body="<p>Your brief history export is complete. No sent briefs were found for your account.</p>",
        )
    except Exception as exc:
        logger.error("Failed to send empty export email to %s: %s", to, exc)
