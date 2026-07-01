"""HTTP API for invoking the analyst-estimates refresh — the cron entrypoint.

Replaces the old ``scripts/sync_estimates.py``: instead of a one-off script, the
refresh is a use case (``SyncAnalystEstimates``) invoked over HTTP, so a scheduler
(the sync-estimates GitHub workflow, or any cron) drives it by POSTing here. The
endpoint runs the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live yfinance (Yahoo) adapter +
the SQL repository and hand them to the use case. yfinance reads Yahoo's public data
with no API key, so — unlike the old FMP path — there's no credential to gate on; the
sync is always constructable.

Security: this endpoint is intentionally **unauthenticated**. It writes the database
(and hits Yahoo), so it relies on network isolation — it must not be reachable from the
public internet. Put a guard (auth token / private networking) in front before
exposing it.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_estimates_adapter import YfinanceEstimatesProvider
from app.stocks.estimates.db_repository import SqlAnalystEstimatesRepository
from app.stocks.estimates.use_cases import EstimatesSyncReport, SyncAnalystEstimates

router = APIRouter(tags=["estimates-cron"])


class EstimatesSyncResponse(BaseModel):
    """The refresh run's summary: rows renewed, rows the vendor couldn't serve this
    run, and the per-run cap that was applied."""

    refreshed: int
    failed: int
    limit: int


def get_sync_estimates(db: Session = Depends(get_db)) -> SyncAnalystEstimates:
    # The refresh reads Yahoo directly (not the DB cache it fills). yfinance needs no
    # key, so there's nothing to gate on — the sync is always wired.
    return SyncAnalystEstimates(
        YfinanceEstimatesProvider(), SqlAnalystEstimatesRepository(db)
    )


def _present(report: EstimatesSyncReport) -> EstimatesSyncResponse:
    """Presenter: use-case result -> HTTP response DTO."""
    return EstimatesSyncResponse(
        refreshed=report.refreshed, failed=report.failed, limit=report.limit
    )


@router.post("/internal/estimates/sync", response_model=EstimatesSyncResponse)
def sync_estimates_endpoint(
    limit: int = Query(
        SyncAnalystEstimates.DEFAULT_LIMIT,
        ge=1,
        le=1000,
        description=(
            "Max stored rows to refresh this run, stalest first. Kept modest so the "
            "sequential Yahoo calls stay gentle on its rate limits."
        ),
    ),
    use_case: SyncAnalystEstimates = Depends(get_sync_estimates),
) -> EstimatesSyncResponse:
    # Runs synchronously: a few hundred sequential Yahoo calls can take a while, so the
    # caller (and any proxy / load balancer in front) must allow a long enough idle
    # timeout, or pass a smaller `limit`.
    report = use_case.execute(limit=limit)
    return _present(report)
