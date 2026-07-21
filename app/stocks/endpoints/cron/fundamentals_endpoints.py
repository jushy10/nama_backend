import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance.fundamentals_adapter_impl import (
    FundamentalsAdapterImpl,
)
from app.stocks.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron.auth import require_cron_token
from app.stocks.catalog.fundamentals.fundamentals_repository_adapter_impl import FundamentalsRepositoryAdapterImpl
from app.stocks.catalog.fundamentals.use_cases import FundamentalsSyncReport, SyncFundamentals

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
    db = SessionLocal()
    try:
        report = SyncFundamentals(
            FundamentalsAdapterImpl(),
            FundamentalsRepositoryAdapterImpl(db),
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
