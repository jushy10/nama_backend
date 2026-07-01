"""HTTP API for invoking the analyst-estimates refresh — the cron entrypoint.

Replaces the old ``scripts/sync_estimates.py``: instead of a one-off script, the
refresh is a use case (``SyncAnalystEstimates``) invoked over HTTP, so a scheduler
(the sync-estimates GitHub workflow, or any cron) drives it by POSTing here. The
endpoint runs the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live FMP adapter + the SQL
repository and hand them to the use case. The FMP key is read from the runtime
environment, so — unlike the old one-off ECS task that received the key as an
override — the *serving* task must carry ``FMP_API_KEY`` for this to work (a missing
key is a 503, like the other key-gated providers).

Security: this endpoint is intentionally **unauthenticated**. It spends FMP quota and
writes the database, so it relies on network isolation — it must not be reachable
from the public internet. Put a guard (auth token / private networking) in front
before exposing it.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.fmp_estimates_adapter import FmpEstimatesProvider
from app.stocks.estimates.stock_estimates_repository import (
    SqlAnalystEstimatesRepository,
)
from app.stocks.estimates.use_cases import EstimatesSyncReport, SyncAnalystEstimates

router = APIRouter(tags=["estimates-cron"])


class EstimatesSyncResponse(BaseModel):
    """The refresh run's summary: rows renewed, rows the vendor couldn't serve this
    run, and the per-run cap that was applied."""

    refreshed: int
    failed: int
    limit: int


def get_sync_estimates(db: Session = Depends(get_db)) -> SyncAnalystEstimates:
    # The refresh reads FMP directly (not the DB cache it fills), so it needs the FMP
    # key in the serving environment. Required here — a sync with no source to read is
    # a hard 503, the same shape as the other key-gated providers.
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise HTTPException(503, "Estimates sync is not configured (FMP_API_KEY).")
    return SyncAnalystEstimates(
        FmpEstimatesProvider(key), SqlAnalystEstimatesRepository(db)
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
            "Max stored rows to refresh this run, stalest first. Held below the "
            "vendor's ~250-calls/day free quota."
        ),
    ),
    use_case: SyncAnalystEstimates = Depends(get_sync_estimates),
) -> EstimatesSyncResponse:
    # Runs synchronously: a few hundred sequential FMP calls can take a while, so the
    # caller (and any proxy / load balancer in front) must allow a long enough idle
    # timeout, or pass a smaller `limit`.
    report = use_case.execute(limit=limit)
    return _present(report)
