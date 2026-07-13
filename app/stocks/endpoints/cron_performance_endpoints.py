"""HTTP API for invoking the stock-performance refresh — the cron entrypoint.

The refresh is a use case (``SyncStockPerformance``) driven over HTTP: a scheduler (the
sync-stock-performance GitHub workflow, or any cron) POSTs here to kick it off. The workflow
launches it as a one-off ECS task via ``python -m app.sync performance`` (reusing
``run_stock_performance_sync`` below); this endpoint stays as the runner's source and for a
manual/HTTP trigger.

The sweep is **fire-and-forget**. It reads the screened anchor and fetches a year of daily
bars for the whole set from Alpaca — minutes of work — but the API Gateway in front of the app
has a hard 30s integration timeout, so a synchronous run would 504 at the gateway while the app
kept working. So the endpoint schedules the sweep on a background thread and returns ``202`` at
once; the shared ``background_sync`` helper owns the threading, the single-flight guard, and the
exception handling. A partial run is safe: ``set_performance`` commits and the sweep is
stale-first, so an interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_stock_performance_sync`` opens a fresh
session and builds the Alpaca batched-performance adapter + the SQL repository for the use case.
Alpaca is the app's price feed, gated on ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY``; unlike
the HTTP price views (which 503 when the keys are unset), a background runner isn't an HTTP
context, so a missing key is logged and the run is a no-op rather than an exception.
``get_sync_runner`` is the DI seam tests override with a fake.

Security: the trigger is guarded by a shared bearer token. The endpoint depends on
``require_cron_token`` (see ``cron_auth``), which requires ``Authorization: Bearer
$CRON_SYNC_TOKEN`` and is **fail-closed** — an unset token is a ``503``, a missing or wrong one
a ``401``. The sync workflow no longer POSTs here (it runs the sweep as a one-off ECS task via
``python -m app.sync``), so this guard only gates the manual / HTTP trigger.
"""

import logging
import os
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.alpaca_adapter import AlpacaStockDataProvider
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.performance.db_repository import SqlPerformanceRepository
from app.stocks.performance.use_cases import PerformanceSyncReport, SyncStockPerformance

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stock-performance-cron"])

# Single-flight guard for the stock-performance sweep only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_stock_performance_sync(limit: int | None) -> PerformanceSyncReport:
    """Perform one full refresh run with its **own** DB session (the request-scoped ``get_db``
    one is closed by the time the background thread runs).

    Builds the Alpaca batched-performance adapter from the env keys. Unlike the HTTP price
    views, a background runner isn't an HTTP context, so unset keys are logged and the run is a
    no-op (an empty report) rather than a 503."""
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        logger.warning(
            "stock performance sync: Alpaca keys unset "
            "(APCA_API_KEY_ID / APCA_API_SECRET_KEY); nothing to do"
        )
        return PerformanceSyncReport(refreshed=0, skipped=0, limit=limit)
    db = SessionLocal()
    try:
        report = SyncStockPerformance(
            AlpacaStockDataProvider(key, secret),
            SqlPerformanceRepository(db),
        ).execute(limit=limit)
        logger.info(
            "stock performance sync done: refreshed=%d skipped=%d limit=%s",
            report.refreshed,
            report.skipped,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_stock_performance_sync


@router.post(
    "/internal/performance/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_stock_performance_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on screened stocks refreshed this run (un-synced first, then "
            "stalest). Omit to process every screened stock — the default; the batched feed "
            "makes a full sweep cheap."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="stock performance sync")
