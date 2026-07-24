import logging

from app.domains.coverage.recommendations.entities import AnalystRecommendations
from app.domains.coverage.recommendations.interfaces import RecommendationAdapter
from app.domains.coverage.recommendations.repository import RecommendationsRepository

logger = logging.getLogger(__name__)


class RecommendationAdapterImpl(RecommendationAdapter):
    def __init__(
        self,
        inner: RecommendationAdapter,
        repo: RecommendationsRepository,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A
        # live failure here has nothing to fall back on, so it propagates (→ 502).
        recommendations = self._inner.get_recommendations(symbol)
        if not recommendations.is_empty:
            self._safe_upsert(symbol, recommendations)
        return recommendations

    def _safe_get(self, symbol: str) -> AnalystRecommendations | None:
        # A cache read must never break the recommendations: on any error, treat it as a
        # miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "recommendations cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, recommendations: AnalystRecommendations) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller
        # already has a good answer for. (Name comes from the sync job, not this feed, so
        # it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, recommendations)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "recommendations cache write failed for %s", symbol, exc_info=True
            )
