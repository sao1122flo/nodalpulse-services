"""Bearer-token auth dependency for protected API routes.

Two principal kinds:
  - internal: the shared services_api_key (web layer → services, scheduler → services)
  - user:     a user-issued API key in np_<prefix>_<secret> format (Org-tier feature)

All existing routes use `dependencies=[Depends(verify_bearer)]` and ignore the
return value — changing to Principal is backwards-compatible.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.settings import settings

logger = logging.getLogger(__name__)
_bearer = HTTPBearer()


@dataclass
class Principal:
    kind: Literal["internal", "user"]
    user_id: str | None = None


async def _resolve_user_api_key(credential: str) -> "Principal | None":
    """Look up a user API key by prefix + SHA-256 hash. Returns None on miss."""
    # Format: np_<8-char-prefix>_<32-char-secret>
    parts = credential.split("_")
    if len(parts) != 3 or parts[0] != "np":
        return None

    prefix = parts[1]
    key_hash = hashlib.sha256(credential.encode()).hexdigest()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, user_id
                FROM api_keys
                WHERE key_prefix = :prefix
                  AND key_hash    = :key_hash
                  AND revoked_at IS NULL
            """),
            {"prefix": prefix, "key_hash": key_hash},
        )
        row = result.mappings().first()

    if not row:
        return None

    # Update last_used_at fire-and-forget — failure is non-fatal.
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"),
                {"id": str(row["id"])},
            )
            await session.commit()
    except Exception as exc:
        logger.warning("Failed to update api_key.last_used_at: %s", exc)

    return Principal(kind="user", user_id=str(row["user_id"]))


async def verify_bearer(
    creds: HTTPAuthorizationCredentials = Security(_bearer),
) -> Principal:
    credential = creds.credentials

    # Internal service key (web → services, scheduler → services).
    if settings.services_api_key and secrets.compare_digest(credential, settings.services_api_key):
        return Principal(kind="internal")

    # User API key.
    if credential.startswith("np_"):
        principal = await _resolve_user_api_key(credential)
        if principal:
            return principal

    raise HTTPException(status_code=401, detail="Unauthorized")
