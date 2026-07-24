"""The recommendations slice's composition root — the endpoint and the cron runner call
build_*(db) and receive a finished use case; all construction knowledge lives here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.db.db_cached_rating_change_adapter_impl import (
    RatingChangeAdapterImpl as DbCachedRatingChangeAdapterImpl,
)
from app.adapters.db.db_cached_recommendation_adapter_impl import (
    RecommendationAdapterImpl as DbCachedRecommendationAdapterImpl,
)
from app.adapters.yfinance.rating_change_adapter_impl import (
    RatingChangeAdapterImpl as YfinanceRatingChangeAdapterImpl,
)
from app.adapters.yfinance.recommendation_adapter_impl import (
    RecommendationAdapterImpl as YfinanceRecommendationAdapterImpl,
)
from app.domains.coverage.recommendations.db_repository import (
    DbRatingChangesRepository,
    DbRecommendationsRepository,
)
from app.domains.coverage.recommendations.interfaces import (
    RatingChangeAdapter,
    RecommendationAdapter,
)
from app.domains.coverage.recommendations.use_cases import (
    GetStockAnalystInfo,
    SyncRecommendations,
)


@lru_cache(maxsize=1)
def get_live_recommendation_provider() -> RecommendationAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceRecommendationAdapterImpl()


@lru_cache(maxsize=1)
def get_live_rating_change_provider() -> RatingChangeAdapter:
    # Its rating-change sibling — same singleton rationale.
    return YfinanceRatingChangeAdapterImpl()


def build_get_stock_analyst_info(db: Session) -> GetStockAnalystInfo:
    # Persistent DB caches (refreshed out of band by the recommendations cron + lazily on
    # a miss) sit in front of Yahoo so the read rarely calls it, and stored rows serve
    # without a live round-trip. yfinance needs no key, so this is always wired.
    recommendations = DbCachedRecommendationAdapterImpl(
        get_live_recommendation_provider(), DbRecommendationsRepository(db)
    )
    rating_changes = DbCachedRatingChangeAdapterImpl(
        get_live_rating_change_provider(), DbRatingChangesRepository(db)
    )
    return GetStockAnalystInfo(recommendations, rating_changes)


def build_sync_recommendations(db: Session) -> SyncRecommendations:
    # The sweep talks to Yahoo directly — refreshing the stored rows is its whole point.
    # Rating changes ride the same sweep (best-effort) rather than a second anchor pass.
    return SyncRecommendations(
        get_live_recommendation_provider(),
        DbRecommendationsRepository(db),
        rating_change_provider=get_live_rating_change_provider(),
        rating_change_repository=DbRatingChangesRepository(db),
    )
