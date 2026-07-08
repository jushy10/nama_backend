"""HTTP API for invoking the ETF refresh — the cron entrypoint.

The refresh is a use case (``SyncEtfs``) driven over HTTP: a scheduler (a GitHub workflow, or
any cron) POSTs here to kick it off.

Like the other ``/internal/*/sync`` endpoints it's **fire-and-forget** — it schedules the sweep
on a background thread and returns ``202`` at once, so the sweep can't blow API Gateway's hard
30s integration timeout. The shared ``background_sync`` helper owns the threading, the
single-flight guard, and the exception handling (see it for the full rationale and the
per-process-guard caveat). The ETF sweep is two passes: a bulk screen-and-upsert, then a
per-ticker profile enrichment (a few hundred sequential Yahoo ``.info`` + ``funds_data`` calls —
minutes, well past 30s), the same shape as the universe sweep. ``limit`` caps only the enrichment
pass; the screen always runs in full. A partial run is safe — the screen upsert and each profile
write commit independently, and the profile write is merge-preserving, so an interrupted run
resumes next trigger without losing stored data.

Wiring lives here, the composition-root way: ``run_etf_sync`` opens a fresh session and builds
the live yfinance screener + profile adapters and the SQL repository for the use case. Yahoo
needs no API key, so there's no credential to gate on; the sync is always constructable.
``get_sync_runner`` is the DI seam tests override with a fake.

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
    """Perform one full ETF sync (screen + upsert + profile enrichment) with its **own** DB
    session (the request-scoped ``get_db`` one is closed by the time the background thread runs).
    ``limit`` caps the enrichment pass only; the screen always runs in full."""
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
                "etf sync done: screened=%d added=%d updated=%d enriched=%d enrich_failed=%d",
                report.screened,
                report.added,
                report.updated,
                report.enriched,
                report.enrich_failed,
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
