import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance.company_classification_adapter_impl import (
    CompanyClassificationAdapterImpl,
)
from app.stocks.adapters.yfinance.stock_screener_adapter_impl import StockScreenerAdapterImpl
from app.stocks.company.earnings.quarterly.quarterly_earnings_repository_adapter_impl import QuarterlyEarningsRepositoryAdapterImpl
from app.stocks.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron.auth import require_cron_token
from app.stocks.catalog.universe.repository_adapter_impl import UniverseRepositoryAdapterImpl
from app.stocks.catalog.universe.use_cases import SyncUniverse, UniverseSyncReport

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
    report = SyncUniverse(
        StockScreenerAdapterImpl(),
        UniverseRepositoryAdapterImpl(db),
        CompanyClassificationAdapterImpl(),
        QuarterlyEarningsRepositoryAdapterImpl(db),
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
