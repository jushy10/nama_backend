import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_etf_profile_adapter import (
    YfinanceEtfProfileProvider,
)
from app.stocks.adapters.yfinance_etf_screener_adapter import (
    YfinanceEtfScreenerProvider,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.etfs.db_repository import SqlEtfRepository
from app.stocks.etfs.use_cases import EtfSyncReport, SyncEtfs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["etf-cron"])

# Single-flight guard for the ETF sweep only — independent of the other cron slices, which may
# run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_etf_sync(limit: int | None) -> EtfSyncReport:
    db = SessionLocal()
    try:
        report = SyncEtfs(
            YfinanceEtfScreenerProvider(),
            SqlEtfRepository(db),
            YfinanceEtfProfileProvider(),
        ).execute(limit=limit)
        if report.skipped:
            logger.warning(
                "etf sync skipped: screen came back too small (screened=%d) — nothing "
                "written (Yahoo blocked?)",
                report.screened,
            )
        else:
            logger.info(
                "etf sync done: screened=%d added=%d updated=%d enriched=%d "
                "enrich_failed=%d without_holdings=%d",
                report.screened,
                report.added,
                report.updated,
                report.enriched,
                report.enrich_failed,
                report.enriched_without_holdings,
            )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_etf_sync


@router.post(
    "/internal/etfs/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_etfs_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        le=2000,
        description=(
            "Max funds whose profile the background sweep refreshes this run, via a per-ticker "
            "Yahoo call. Omit to refresh EVERY stored fund in one run (the default); pass a value "
            "only to throttle a run if Yahoo starts rate-limiting. The screen itself always runs "
            "in full regardless."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once, or
    # 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="etf sync")
