"""HTTP API for invoking the quarterly-earnings refresh — the cron entrypoint.

The refresh is a use case (``SyncQuarterlyEarnings``) driven over HTTP: a scheduler (the
sync-quarterly-earnings GitHub workflow, or any cron) POSTs here to kick it off.

The sweep is **fire-and-forget**. A few hundred sequential Yahoo calls take minutes, but the
API Gateway in front of the app has a hard 30s integration timeout — a synchronous run would
504 at the gateway while the app kept working. So the endpoint schedules the sweep on a
background thread and returns ``202`` at once; the shared ``background_sync`` helper owns the
threading, the single-flight guard, and the exception handling (see it for the full rationale
and the per-process-guard caveat). A partial run is safe: ``upsert`` commits per stock and the
sweep is stalest-first, so an interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_quarterly_earnings_sync`` opens a fresh
session and builds the live yfinance adapter + the SQL repository for the use case. yfinance
reads Yahoo's public data with no API key, so there's no credential to gate on; the sync is
always constructable. ``get_sync_runner`` is the DI seam tests override with a fake.

Security: this endpoint is currently **unauthenticated** — it writes the database (and hits
Yahoo) and is triggered over the public internet by the sync workflow, so an auth token
(planned: a shared ``CRON_SYNC_TOKEN`` bearer guard) should be added before the endpoints are
considered hardened.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_quarterly_earnings_adapter import (
    YfinanceQuarterlyEarningsProvider,
)
from app.stocks.earnings.quarterly.db_repository import SqlQuarterlyEarningsRepository
from app.stocks.earnings.quarterly.use_cases import (
    QuarterlyEarningsSyncReport,
    SyncQuarterlyEarnings,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.sync_progress import (
    HeartbeatReporter,
    progress_interval_seconds,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["quarterly-earnings-cron"])

# Single-flight guard for the quarterly sweep only — independent of the other cron slices,
# which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_quarterly_earnings_sync(limit: int | None) -> QuarterlyEarningsSyncReport:
    """Perform one full refresh run with its **own** DB session (the request-scoped
    ``get_db`` one is closed by the time the background thread runs)."""
    db = SessionLocal()
    try:
        with HeartbeatReporter(
            "quarterly-earnings sync", logger, interval_s=progress_interval_seconds()
        ) as reporter:
            report = SyncQuarterlyEarnings(
                YfinanceQuarterlyEarningsProvider(), SqlQuarterlyEarningsRepository(db)
            ).execute(limit=limit, progress=reporter)
        logger.info(
            "quarterly-earnings sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_quarterly_earnings_sync


@router.post(
    "/internal/earnings/quarterly/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_quarterly_earnings_endpoint(
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
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="quarterly-earnings sync")
