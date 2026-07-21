import logging
import threading

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.yfinance.rating_change_adapter_impl import (
    RatingChangeAdapterImpl,
)
from app.stocks.adapters.yfinance.recommendation_adapter_impl import (
    RecommendationAdapterImpl,
)
from app.stocks.company.recommendations.repository_adapter_impl import (
    RatingChangesRepositoryAdapterImpl,
    RecommendationsRepositoryAdapterImpl,
)
from app.stocks.company.recommendations.use_cases import (
    RecommendationsSyncReport,
    SyncRecommendations,
)
from app.stocks.endpoints.cron.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron.auth import require_cron_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["recommendations-cron"])

# Single-flight guard for the recommendations sweep only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_recommendations_sync(limit: int | None) -> RecommendationsSyncReport:
    db = SessionLocal()
    try:
        report = SyncRecommendations(
            RecommendationAdapterImpl(),
            RecommendationsRepositoryAdapterImpl(db),
            rating_change_provider=RatingChangeAdapterImpl(),
            rating_change_repository=RatingChangesRepositoryAdapterImpl(db),
        ).execute(limit=limit)
        logger.info(
            "recommendations sync done: refreshed=%d rating_changes=%d failed=%d limit=%s",
            report.refreshed,
            report.rating_changes_refreshed,
            report.failed,
            report.limit,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    return run_recommendations_sync


@router.post(
    "/internal/recommendations/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_recommendations_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Optional cap on stocks refreshed this run (un-cached first, then stalest). "
            "Omit to process every stock in the anchor — the default; pass a value to "
            "throttle the sequential Yahoo calls."
        ),
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the sweep on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, limit, response, label="recommendations sync")
