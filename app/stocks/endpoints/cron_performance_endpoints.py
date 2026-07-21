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
