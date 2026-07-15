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

Wiring lives here, the composition-root way: ``run_universe_sync`` opens a fresh session and,
for each market in turn (US then Canada), builds the live yfinance screener + classification
adapters, the universe SQL repository, and the quarterly-earnings cache repository (the DB-only
TTM read the valuation pass pairs with the screen-time price to derive each stock's stored P/E)
for the use case. The two passes are independent additive upserts onto the shared anchor, so a
bad day for one market never touches the other's rows. Yahoo needs no API key, so there's no
credential to gate on; the sync is always constructable. ``get_sync_runner`` is the DI seam
tests override with a fake.

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
from app.stocks.adapters.yfinance_classification_adapter import (
    YfinanceClassificationProvider,
)
from app.stocks.adapters.yfinance_screener_adapter import YfinanceScreenerProvider
from app.stocks.earnings.quarterly.db_repository import SqlQuarterlyEarningsRepository
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport

logger = logging.getLogger(__name__)
router = APIRouter(tags=["universe-cron"])

# Single-flight guard for the universe sweep only — independent of the other cron slices,
# which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


# The markets screened each run, in order. US first (the large pass), then Canada (TSX/TSXV).
# Each pass is an independent additive upsert onto the shared anchor, so a bad day for one
# market never touches the other's stored rows.
_REGIONS = ("us", "ca")


def _run_universe_pass(db, *, region: str, limit: int) -> UniverseSyncReport:
    """Run one market's screen+enrich+value pass over the shared session, logging its outcome."""
    report = SyncUniverse(
        YfinanceScreenerProvider(),
        SqlUniverseRepository(db),
        YfinanceClassificationProvider(),
        SqlQuarterlyEarningsRepository(db),
        region=region,
    ).execute(limit=limit)
    if report.skipped:
        logger.warning(
            "universe sync (%s) skipped: screen came back too small (screened=%d) — "
            "nothing written (Yahoo blocked?)",
            region,
            report.screened,
        )
    else:
        logger.info(
            "universe sync (%s) done: screened=%d added=%d updated=%d enriched=%d "
            "enrich_failed=%d valued=%d limit=%d",
            region,
            report.screened,
            report.added,
            report.updated,
            report.enriched,
            report.enrich_failed,
            report.valued,
            limit,
        )
    return report


def run_universe_sync(limit: int) -> UniverseSyncReport:
    """Perform one full sync run — the US screen then the Canadian screen — with its **own** DB
    session (the request-scoped ``get_db`` one is closed by the time the background thread runs).

    The two passes are independent: a hard failure or degraded (skipped) screen in one market
    doesn't stop the other, and each commits its own writes. Returns a single report aggregating
    both passes (counts summed; ``skipped`` true only when *every* pass was skipped) so the
    fire-and-forget caller has one summary to log."""
    db = SessionLocal()
    try:
        reports = []
        for region in _REGIONS:
            try:
                reports.append(_run_universe_pass(db, region=region, limit=limit))
            except Exception:  # noqa: BLE001 — one market's failure must not sink the other
                logger.exception("universe sync (%s) failed", region)
        return _merge_reports(reports)
    finally:
        db.close()


def _merge_reports(reports: list[UniverseSyncReport]) -> UniverseSyncReport:
    """Sum the per-market counts into one report. ``skipped`` is true only when every pass was
    skipped (or none ran) — a mixed run (US written, CA skipped) is not a skip."""
    if not reports:
        return UniverseSyncReport(
            screened=0, added=0, updated=0, skipped=True,
            enriched=0, enrich_failed=0, valued=0,
        )
    return UniverseSyncReport(
        screened=sum(r.screened for r in reports),
        added=sum(r.added for r in reports),
        updated=sum(r.updated for r in reports),
        skipped=all(r.skipped for r in reports),
        enriched=sum(r.enriched for r in reports),
        enrich_failed=sum(r.enrich_failed for r in reports),
        valued=sum(r.valued for r in reports),
    )


def get_sync_runner() -> SyncRunner:
    """DI seam for the sweep's unit of work; tests override it with a fake."""
    return run_universe_sync


@router.post(
    "/internal/universe/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
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
