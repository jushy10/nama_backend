"""HTTP API for invoking the universe refresh — the cron entrypoint.

The refresh is a use case (``SyncUniverse``) driven over HTTP: a scheduler (the sync-universe
GitHub workflow, or any cron) POSTs here to kick it off.

The sweep is **fire-and-forget**. It used to run synchronously — a handful of fast bulk screen
pages, safely inside API Gateway's hard 30s integration timeout. That changed when the sync
gained a second pass: after the screen, it enriches each still-unclassified stock's
sector/industry through a *per-ticker* Yahoo ``.info`` call (the bulk screen carries neither),
and a few hundred to a few thousand sequential calls take minutes — well past 30s. So the
endpoint now schedules the sweep on a background thread and returns ``202`` at once; the shared
``background_sync`` helper owns the threading, the single-flight guard, and the exception
handling (see it for the full rationale and the per-process-guard caveat). A partial run is
safe: the screen upsert and each enrichment write commit independently, and the enrichment is
fill-once, so an interrupted run just resumes on the next trigger.

Wiring lives here, the composition-root way: ``run_universe_sync`` opens a fresh session and
builds the live yfinance screener + classification adapters and the SQL repository for the use
case. Yahoo needs no API key, so there's no credential to gate on; the sync is always
constructable. ``get_sync_runner`` is the DI seam tests override with a fake.

Security: this endpoint is currently **unauthenticated** — it writes the database (and hits
Yahoo) and is triggered over the public internet by the sync workflow, so an auth token
(planned: a shared ``CRON_SYNC_TOKEN`` bearer guard) should be added before the endpoints are
considered hardened.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance_classification_adapter import (
    YfinanceClassificationProvider,
)
from app.stocks.adapters.yfinance_screener_adapter import YfinanceScreenerProvider
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.sync_progress import (
    HeartbeatReporter,
    progress_interval_seconds,
)
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport

logger = logging.getLogger(__name__)
router = APIRouter(tags=["universe-cron"])

# Single-flight guard for the universe sweep only — independent of the other cron slices,
# which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_universe_sync(limit: int) -> UniverseSyncReport:
    """Perform one full sync run (screen + enrich) with its **own** DB session (the request-
    scoped ``get_db`` one is closed by the time the background thread runs)."""
    db = SessionLocal()
    try:
        # The screen is a handful of fast pages; the heartbeat tracks the slow half — the
        # per-ticker enrichment pass. Log the screen phase so CloudWatch shows the run is live
        # before the first enrichment tick.
        logger.info("universe sync: screening the US market, then enriching classifications")
        with HeartbeatReporter(
            "universe sync (enrichment)", logger, interval_s=progress_interval_seconds()
        ) as reporter:
            report = SyncUniverse(
                YfinanceScreenerProvider(),
                SqlUniverseRepository(db),
                YfinanceClassificationProvider(),
            ).execute(limit=limit, progress=reporter)
        if report.skipped:
            logger.warning(
                "universe sync skipped: screen came back too small (screened=%d) — "
                "nothing written (Yahoo blocked?)",
                report.screened,
            )
        else:
            logger.info(
                "universe sync done: screened=%d added=%d updated=%d enriched=%d "
                "enrich_failed=%d limit=%d",
                report.screened,
                report.added,
                report.updated,
                report.enriched,
                report.enrich_failed,
                limit,
            )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_universe_sync


@router.post(
    "/internal/universe/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_universe_endpoint(
    response: Response,
    limit: int = Query(
        SyncUniverse.DEFAULT_LIMIT,
        ge=1,
        le=3000,
        description=(
            "Max stocks whose sector/industry the background sweep classifies this run, via a "
            "per-ticker Yahoo call. The market screen itself always runs in full; only the "
            "enrichment pass is capped, so a universe larger than this is classified over "
            "successive runs. Kept bounded to stay gentle on Yahoo's rate limits."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="universe sync")
