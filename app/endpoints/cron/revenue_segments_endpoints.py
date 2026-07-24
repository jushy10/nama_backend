import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.domains.financials.revenue_segments import wiring
from app.domains.financials.revenue_segments.use_cases import (
    RevenueSegmentsSyncReport,
)
from app.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.endpoints.cron.auth import require_cron_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["revenue-segments-cron"])

# Single-flight guard for the revenue-segments sweep only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_revenue_segments_sync(limit: int | None) -> RevenueSegmentsSyncReport:
    db = SessionLocal()
    try:
        report = wiring.build_sync_revenue_segments(db).run(limit=limit)
        logger.info(
            "revenue-segments sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_revenue_segments_sync


@router.post(
    "/internal/revenue-segments/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_revenue_segments_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on stocks refreshed this run (un-cached first, then stalest). "
            "Omit to process every stock in the anchor — the default; pass a value to "
            "throttle the sequential SEC calls."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once, or
    # 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="revenue-segments sync")
