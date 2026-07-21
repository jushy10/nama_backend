import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ``auto_error=False`` so a missing or non-bearer Authorization header yields ``None`` here
# instead of ``HTTPBearer`` raising its own 403 — we want one uniform 401 (with a
# ``WWW-Authenticate`` challenge) for every "no valid token" case, and the 503 "not configured"
# check to take precedence over it.
_bearer = HTTPBearer(
    auto_error=False,
    description="Shared cron sync token: send as `Authorization: Bearer $CRON_SYNC_TOKEN`.",
)


def require_cron_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    expected = os.environ.get("CRON_SYNC_TOKEN")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Cron sync is not configured (CRON_SYNC_TOKEN).",
        )
    provided = credentials.credentials if credentials else ""
    # Compare as bytes: secrets.compare_digest rejects str with non-ASCII, and encoding both
    # sides sidesteps that entirely while staying constant-time.
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing cron sync token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
