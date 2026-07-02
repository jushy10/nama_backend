"""Shared bearer-token guard for the cron endpoints.

The sync endpoints write the database and hit Yahoo, and they're triggered over the
public internet by the GitHub sync workflows — so they need *some* gate. The guard is
opt-in via the ``CRON_SYNC_TOKEN`` env var: when set, a request must carry
``Authorization: Bearer <token>``; when unset, the endpoints stay open (local dev, or
a deployment that hasn't configured the token yet), matching their prior behavior.

Ops: set the same value in the app's environment (SSM → ECS task) and as the GitHub
Actions secret the sync workflows send.
"""

import hmac
import os

from fastapi import Header, HTTPException


def require_cron_token(authorization: str | None = Header(None)) -> None:
    """FastAPI dependency: reject the request unless it carries the cron token.

    A no-op while ``CRON_SYNC_TOKEN`` is unset. Uses a constant-time comparison so
    the token can't be guessed byte-by-byte through timing.
    """
    expected = os.environ.get("CRON_SYNC_TOKEN")
    if not expected:
        return
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(401, "Missing or invalid cron token.")
