"""HTTP API for invoking the fundamentals refresh — the cron entrypoint.

The refresh is a use case (``SyncFundamentals``) driven over HTTP: a scheduler (the
sync-fundamentals GitHub workflow, or any cron) POSTs here to kick it off.

The sweep is **fire-and-forget**. Walking the anchor a stock at a time (one Yahoo ``.info``
read each) takes minutes, but the API Gateway in front of the app has a hard 30s integration
timeout — a synchronous run would 504 at the gateway while the app kept working. So the
endpoint schedules the sweep on a background thread and returns ``202`` at once; the shared
``background_sync`` helper owns the threading, the single-flight guard, and the exception
handling. A partial run is safe: ``upsert`` commits per stock and the sweep is stale-first, so
an interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_fundamentals_sync`` opens a fresh session
and builds the live yfinance adapter + the SQL repository for the use case. yfinance reads
Yahoo's public data with no API key, so there's no credential to gate on; the sync is always
constructable. ``get_sync_runner`` is the DI seam tests override with a fake.

Security: the trigger is guarded by a shared bearer token. The endpoint depends on
``require_cron_token`` (see ``cron_auth``), which requires ``Authorization: Bearer
$CRON_SYNC_TOKEN`` and is **fail-closed** — an unset token is a ``503``, a missing or wrong one
a ``401``. The sync workflow no longer POSTs here (it runs the sweep as a one-off ECS task via
``python -m app.sync``), so this guard only gates the manual / HTTP trigger.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_fundamentals_adapter import (
    YfinanceFundamentalsProvider,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.fundamentals.db_repository import SqlFundamentalsRepository
from app.stocks.fundamentals.use_cases import FundamentalsSyncReport, SyncFundamentals

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fundamentals-cron"])

# Single-flight guard for the fundamentals sweep only — independent of the other cron slices,
# which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()

# Pause between the sync's retry passes in production. The use case defaults this to 0 (so the
# offline tests never sleep); here — the composition root — we dial it up so an intermittent
# Yahoo block has ~30s to lift before a gated symbol is re-attempted. A batch run isn't behind
# the API Gateway's 30s clock (it's a one-off ECS task), so the added seconds are free.
_RETRY_BACKOFF_SECONDS = 30.0


def run_fundamentals_sync(limit: int | None) -> FundamentalsSyncReport:
    """Perform one full refresh run with its **own** DB session (the request-scoped ``get_db``
    one is closed by the time the background thread runs)."""
    db = SessionLocal()
    try:
        report = SyncFundamentals(
            YfinanceFundamentalsProvider(),
            SqlFundamentalsRepository(db),
            retry_backoff_seconds=_RETRY_BACKOFF_SECONDS,
        ).execute(limit=limit)
        logger.info(
            "fundamentals sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_fundamentals_sync


@router.post(
    "/internal/fundamentals/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_fundamentals_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on stocks refreshed this run (un-synced first, then stalest). "
            "Omit to process every stock in the anchor — the default; pass a value to "
            "throttle the sequential Yahoo calls."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="fundamentals sync")
