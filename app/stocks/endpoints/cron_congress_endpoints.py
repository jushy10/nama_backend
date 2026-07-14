"""HTTP API for invoking the Congressional-trades refresh — the cron entrypoint.

The refresh is a use case (``SyncCongressTrades``) driven over HTTP: a scheduler (the sync-congress
GitHub workflow, or any cron) POSTs here to kick it off.

The sweep is **fire-and-forget**. It downloads the whole market-wide feed (several megabytes) and
distributes it across the anchor, which takes longer than the API Gateway's hard 30s integration
timeout allows — a synchronous run would 504 at the gateway while the app kept working. So the
endpoint schedules the sweep on a background thread and returns ``202`` at once; the shared
``background_sync`` helper owns the threading, the single-flight guard, and the exception handling.
A partial run is safe: ``upsert`` commits per stock and the sweep is stalest-first, so an
interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_congress_sync`` opens a fresh session and builds
the live stock-watcher adapter + the SQL repository for the use case. The source is keyless public
JSON, so there's no credential to gate on; the sync is always constructable. ``get_sync_runner`` is
the DI seam tests override with a fake.

Security: the trigger is guarded by a shared bearer token (``require_cron_token``) — fail-closed, a
``503`` when the token is unset and a ``401`` on a missing/wrong one. The sync workflow no longer
POSTs here (it runs the sweep as a one-off ECS task via ``python -m app.sync``), so this guard only
gates the manual / HTTP trigger.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.stock_watcher_congress_adapter import (
    StockWatcherCongressProvider,
)
from app.stocks.congress.db_repository import SqlCongressTradesRepository
from app.stocks.congress.use_cases import CongressSyncReport, SyncCongressTrades
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["congress-cron"])

# Single-flight guard for the congress sweep only — independent of the other cron slices, which may
# run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()

# A small courtesy spacing between the (two, large) feed downloads. A batch run isn't behind the API
# Gateway's 30s clock (it's a one-off ECS task), so the added spacing is free.
_FEED_MIN_REQUEST_INTERVAL = 0.5


def run_congress_sync(limit: int | None) -> CongressSyncReport:
    """Perform one full refresh run with its **own** DB session (the request-scoped ``get_db`` one
    is closed by the time the background thread runs)."""
    db = SessionLocal()
    try:
        report = SyncCongressTrades(
            StockWatcherCongressProvider(
                min_request_interval_seconds=_FEED_MIN_REQUEST_INTERVAL
            ),
            SqlCongressTradesRepository(db),
        ).execute(limit=limit)
        logger.info(
            "congress sync done: fetched=%d stored=%d failed=%d limit=%s",
            report.fetched,
            report.stored,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_congress_sync


@router.post(
    "/internal/congress/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_congress_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on anchor stocks visited this run (un-cached first, then stalest). "
            "Omit to visit every stock in the anchor — the default. The whole feed is fetched "
            "once regardless; the cap only bounds how many stocks it's distributed to this run."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once, or 200
    # "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="congress sync")
