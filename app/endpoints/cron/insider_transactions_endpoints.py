import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.adapters.sec_edgar.insider_transactions_adapter_impl import (
    InsiderTransactionsAdapterImpl,
)
from app.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.endpoints.cron.auth import require_cron_token
from app.domains.ownership.insider_transactions.insider_transactions_repository_adapter_impl import (
    InsiderTransactionsRepositoryAdapterImpl,
)
from app.domains.ownership.insider_transactions.use_cases import (
    InsiderTransactionsSyncReport,
    SyncInsiderTransactions,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["insider-transactions-cron"])

# Single-flight guard for the insider-transactions sweep only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()

# Minimum spacing between the sweep's SEC requests, so the serial walk stays under EDGAR's ~10
# req/s fair-use ceiling. A batch run isn't behind the API Gateway's 30s clock (it's a one-off
# ECS task), so the added spacing is free.
_SEC_MIN_REQUEST_INTERVAL = 0.15


def run_insider_transactions_sync(limit: int | None) -> InsiderTransactionsSyncReport:
    db = SessionLocal()
    try:
        report = SyncInsiderTransactions(
            InsiderTransactionsAdapterImpl(
                min_request_interval_seconds=_SEC_MIN_REQUEST_INTERVAL
            ),
            InsiderTransactionsRepositoryAdapterImpl(db),
        ).execute(limit=limit)
        logger.info(
            "insider-transactions sync done: refreshed=%d failed=%d limit=%s",
            report.refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_insider_transactions_sync


@router.post(
    "/internal/insider-transactions/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_insider_transactions_endpoint(
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
    return trigger_sync(
        _sync_lock, run, limit, response, label="insider-transactions sync"
    )
