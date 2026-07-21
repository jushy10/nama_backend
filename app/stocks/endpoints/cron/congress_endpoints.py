import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.stock_watcher_congress_adapter import (
    StockWatcherCongressProvider,
)
from app.stocks.company.congress.db_repository import SqlCongressTradesRepository
from app.stocks.company.congress.use_cases import CongressSyncReport, SyncCongressTrades
from app.stocks.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron.auth import require_cron_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["congress-cron"])

# Single-flight guard for the congress sweep only — independent of the other cron slices, which may
# run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()

# A small courtesy spacing between the (two, large) feed downloads. A batch run isn't behind the API
# Gateway's 30s clock (it's a one-off ECS task), so the added spacing is free.
_FEED_MIN_REQUEST_INTERVAL = 0.5


def run_congress_sync(limit: int | None) -> CongressSyncReport:
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
