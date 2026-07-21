import logging
import threading

from fastapi import APIRouter, Depends, Response, status

from app.db import SessionLocal
from app.stocks.adapters.wikipedia.index_membership_adapter_impl import (
    IndexMembershipAdapterImpl,
)
from app.stocks.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron.auth import require_cron_token
from app.stocks.catalog.index_membership.index_membership_repository_adapter_impl import IndexMembershipRepositoryAdapterImpl
from app.stocks.catalog.index_membership.use_cases import (
    IndexMembershipSyncReport,
    SyncIndexMembership,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["index-membership-cron"])

# Single-flight guard for the index-membership reconcile only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_index_membership_sync(_limit: int) -> IndexMembershipSyncReport:
    db = SessionLocal()
    try:
        report = SyncIndexMembership(
            IndexMembershipAdapterImpl(), IndexMembershipRepositoryAdapterImpl(db)
        ).execute()
        logger.info(
            "index-membership sync done: sp500 members=%d marked=%d cleared=%d skipped=%s | "
            "nasdaq100 members=%d marked=%d cleared=%d skipped=%s",
            report.sp500_members,
            report.sp500_marked,
            report.sp500_cleared,
            report.sp500_skipped,
            report.nasdaq100_members,
            report.nasdaq100_marked,
            report.nasdaq100_cleared,
            report.nasdaq100_skipped,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_index_membership_sync


@router.post(
    "/internal/index-membership/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_index_membership_endpoint(
    response: Response,
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the reconcile on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. There's no stalest-N here, so the
    # shared helper's ``limit`` is passed as 0 (cosmetic). See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, 0, response, label="index-membership sync")
