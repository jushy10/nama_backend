"""HTTP API for invoking the recommendations refresh — the cron entrypoint.

The refresh is a use case (``SyncRecommendations``) invoked over HTTP, so a scheduler
(the sync-recommendations GitHub workflow, or any cron) drives it by POSTing here. The
endpoint runs the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live yfinance adapter + the SQL
repository and hand them to the use case. yfinance reads Yahoo's public data with no API
key, so there's no credential to gate on; the sync is always constructable.

Security: this endpoint is intentionally **unauthenticated**. It writes the database (and
hits Yahoo), so it relies on network isolation — it must not be reachable from the public
internet. Put a guard (auth token / private networking) in front before exposing it.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_recommendations_adapter import (
    YfinanceRecommendationProvider,
)
from app.stocks.recommendations.db_repository import SqlRecommendationsRepository
from app.stocks.recommendations.use_cases import (
    RecommendationsSyncReport,
    SyncRecommendations,
)

router = APIRouter(tags=["recommendations-cron"])


class RecommendationsSyncResponse(BaseModel):
    """The refresh run's summary: stocks renewed, stocks the vendor couldn't serve (or
    returned empty for) this run, and the per-run cap that was applied."""

    refreshed: int
    failed: int
    limit: int


def get_sync_recommendations(db: Session = Depends(get_db)) -> SyncRecommendations:
    # The refresh reads Yahoo directly (not the DB cache it fills). yfinance needs no key,
    # so there's nothing to gate on — the sync is always wired.
    return SyncRecommendations(
        YfinanceRecommendationProvider(), SqlRecommendationsRepository(db)
    )


def _present(report: RecommendationsSyncReport) -> RecommendationsSyncResponse:
    """Presenter: use-case result -> HTTP response DTO."""
    return RecommendationsSyncResponse(
        refreshed=report.refreshed, failed=report.failed, limit=report.limit
    )


@router.post("/internal/recommendations/sync", response_model=RecommendationsSyncResponse)
def sync_recommendations_endpoint(
    limit: int = Query(
        SyncRecommendations.DEFAULT_LIMIT,
        ge=1,
        le=1000,
        description=(
            "Max stored stocks to refresh this run, stalest first. Kept modest so the "
            "sequential Yahoo calls stay gentle on its rate limits."
        ),
    ),
    use_case: SyncRecommendations = Depends(get_sync_recommendations),
) -> RecommendationsSyncResponse:
    # Runs synchronously: a few hundred sequential Yahoo calls can take a while, so the
    # caller (and any proxy / load balancer in front) must allow a long enough idle
    # timeout, or pass a smaller `limit`.
    report = use_case.execute(limit=limit)
    return _present(report)
