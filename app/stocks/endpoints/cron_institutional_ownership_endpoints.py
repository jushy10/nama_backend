"""HTTP API for invoking the institutional-ownership refresh — the cron entrypoint.

The refresh is a use case (``SyncInstitutionalOwnership``) driven over HTTP: a scheduler (the
sync-institutional-ownership GitHub workflow, or any cron) POSTs here to kick it off.

The sweep is **fire-and-forget**. Hundreds of sequential Yahoo calls take a while, but the API
Gateway in front of the app has a hard 30s integration timeout — a synchronous run would 504 at the
gateway while the app kept working. So the endpoint schedules the sweep on a background thread and
returns ``202`` at once; the shared ``background_sync`` helper owns the threading, the single-flight
guard, and the exception handling. A partial run is safe: ``upsert`` commits per stock and the sweep
is stalest-first, so an interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_institutional_ownership_sync`` opens a fresh
session and builds the live yfinance adapter + the SQL repository for the use case. yfinance reads
Yahoo's public data with no API key, so there's no credential to gate on; the sync is always
constructable. ``get_sync_runner`` is the DI seam tests override with a fake.

Security: the trigger is guarded by a shared bearer token (``require_cron_token``) — fail-closed, a
``503`` when the token is unset and a ``401`` on a missing/wrong one. The sync workflow no longer
POSTs here (it runs the sweep as a one-off ECS task via ``python -m app.sync``), so this guard only
gates the manual / HTTP trigger.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_institutional_holders_adapter import (
    YfinanceInstitutionalHoldersProvider,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.institutional_ownership.db_repository import (
    SqlInstitutionalOwnershipRepository,
)
from app.stocks.institutional_ownership.use_cases import (
    InstitutionalOwnershipSyncReport,
    SyncInstitutionalOwnership,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["institutional-ownership-cron"])

# Single-flight guard for the institutional-ownership sweep only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_institutional_ownership_sync(
    limit: int | None,
) -> InstitutionalOwnershipSyncReport:
    """Perform one full refresh run with its **own** DB session (the request-scoped ``get_db`` one
    is closed by the time the background thread runs)."""
    db = SessionLocal()
    try:
        report = SyncInstitutionalOwnership(
            YfinanceInstitutionalHoldersProvider(),
            SqlInstitutionalOwnershipRepository(db),
        ).execute(limit=limit)
        logger.info(
            "institutional-ownership sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_institutional_ownership_sync


@router.post(
    "/internal/institutional-ownership/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_institutional_ownership_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on stocks refreshed this run (un-cached first, then stalest). "
            "Omit to process every stock in the anchor — the default; pass a value to "
            "throttle the sequential Yahoo calls."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once, or 200
    # "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(
        _sync_lock, run, limit, response, label="institutional-ownership sync"
    )
