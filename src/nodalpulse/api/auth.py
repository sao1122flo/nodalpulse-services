"""Bearer-token auth dependency for protected API routes."""

import secrets

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nodalpulse.settings import settings

_bearer = HTTPBearer()


def verify_bearer(
    creds: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    # Fail closed: reject all requests when key is not configured.
    if not settings.services_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(creds.credentials, settings.services_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized")
