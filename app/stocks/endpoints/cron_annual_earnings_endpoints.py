import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_annual_earnings_adapter import (
    YfinanceAnnualEarningsProvider,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.annual.use_cases import (
    AnnualEarningsSyncReport,
    SyncAnnualEarnings,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["annual-earnings-cron"])

# Single-flight guard for the annual sweep only — independent of the other cron slices,
# which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()

# Pause between the sync's retry passes in production. The use case defaults this to 0 (so the
# offline tests never sleep); here — the composition root — we dial it up so an intermittent
# Yahoo block has ~30s to lift before a blocked symbol is re-attempted. A batch run isn't behind
# the API Gateway's 30s clock (it's a one-off ECS task), so the added seconds are free.
_RETRY_BACKOFF_SECONDS = 30.0


def run_annual_earnings_sync(limit: int | None) -> AnnualEarningsSyncReport:
    db = SessionLocal()
    try:
        report = SyncAnnualEarnings(
            YfinanceAnnualEarningsProvider(),
            SqlAnnualEarningsRepository(db),
            retry_backoff_seconds=_RETRY_BACKOFF_SECONDS,
        ).execute(limit=limit)
        logger.info(
            "annual-earnings sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_annual_earnings_sync


@router.post(
    "/internal/earnings/annual/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_annual_earnings_endpoint(
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
    return trigger_sync(_sync_lock, run, limit, response, label="annual-earnings sync")
