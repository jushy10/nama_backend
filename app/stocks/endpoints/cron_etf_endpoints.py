"""HTTP API for invoking the ETF refresh — the cron entrypoint.

The refresh is a use case (``SyncEtfs``) driven over HTTP: a scheduler (a GitHub workflow, or
any cron) POSTs here to kick it off.

Like the other ``/internal/*/sync`` endpoints it's **fire-and-forget** — it schedules the sweep
on a background thread and returns ``202`` at once, so a slow Yahoo screen can't blow API
Gateway's hard 30s integration timeout. The shared ``background_sync`` helper owns the
threading, the single-flight guard, and the exception handling (see it for the full rationale
and the per-process-guard caveat). The ETF sweep is a single screen-and-upsert (no per-ticker
enrichment, unlike the universe sweep), so it's quick — but it rides the same async machinery
for consistency and 30s-safety. It takes no ``limit`` (the screen always runs in full), so the
runner ignores the helper's limit arg and the response carries ``limit: null``.

Wiring lives here, the composition-root way: ``run_etf_sync`` opens a fresh session and builds
the live yfinance screener + the SQL repository for the use case. Yahoo needs no API key, so
there's no credential to gate on; the sync is always constructable. ``get_sync_runner`` is the
DI seam tests override with a fake.

Security: this endpoint is currently **unauthenticated**, like the other cron endpoints — an
auth-token guard (planned: a shared ``CRON_SYNC_TOKEN`` bearer) should be added before the
endpoints are considered hardened.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_etf_screener_adapter import (
    YfinanceEtfScreenerProvider,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.etfs.db_repository import SqlEtfRepository
from app.stocks.etfs.use_cases import EtfSyncReport, SyncEtfs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["etf-cron"])

# Single-flight guard for the ETF sweep only — independent of the other cron slices, which may
# run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_etf_sync(limit: int | None = None) -> EtfSyncReport:
    """Perform one full ETF sync (screen + upsert) with its **own** DB session (the
    request-scoped ``get_db`` one is closed by the time the background thread runs).

    Accepts the background helper's ``limit`` arg for signature compatibility but ignores it —
    the ETF screen always runs in full and has no enrichment pass to cap.
    """
    db = SessionLocal()
    try:
        report = SyncEtfs(YfinanceEtfScreenerProvider(), SqlEtfRepository(db)).execute()
        if report.skipped:
            logger.warning(
                "etf sync skipped: screen came back too small (screened=%d) — nothing "
                "written (Yahoo blocked?)",
                report.screened,
            )
        else:
            logger.info(
                "etf sync done: screened=%d added=%d updated=%d",
                report.screened,
                report.added,
                report.updated,
            )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_etf_sync


@router.post(
    "/internal/etfs/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_etfs_endpoint(
    response: Response,
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once, or
    # 200 "already_running" if one is already in flight. No limit — the screen runs in full, so
    # the response carries limit: null. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, None, response, label="etf sync")
