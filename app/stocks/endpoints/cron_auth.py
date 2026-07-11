"""Shared bearer-token guard for the fire-and-forget cron sync endpoints.

Every ``/internal/*/sync`` endpoint writes the database (and hits Yahoo / Wikipedia) and is
reachable over the public internet through the API Gateway, so each one depends on
``require_cron_token`` to gate the trigger behind a shared secret. The GitHub sync workflows
do **not** come through here — they run each sweep as a one-off ECS task via
``python -m app.sync <slice>``, calling the ``run_*_sync`` runners directly — so this guard only
protects the HTTP surface, which is now a manual / emergency trigger.

The secret is ``CRON_SYNC_TOKEN``, read from the environment the composition-root way (like
every other credential in this app; see the ``get_*`` factories in ``app/stocks/wiring.py``). The
guard is deliberately **fail-closed**: if the token isn't configured the endpoints return
``503`` and nothing can trigger a sync — the same "missing required credential -> 503" shape the
router uses for the vendor keys. A caller whose bearer token is missing or wrong gets ``401``.
The comparison is constant-time so a wrong token can't be recovered byte-by-byte from response
timing.
"""

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
    """FastAPI dependency: allow the request only if it carries the shared cron token.

    Raises ``503`` when ``CRON_SYNC_TOKEN`` is unset (fail-closed — an unconfigured guard blocks
    everything rather than silently allowing it) and ``401`` when the caller's bearer token is
    missing or doesn't match. Used as a route ``dependencies=[...]`` entry, so it injects nothing
    into the handler; it only gates whether the handler runs.
    """
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
