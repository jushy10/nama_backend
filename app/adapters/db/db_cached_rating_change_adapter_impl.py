import logging

from app.domains.coverage.recommendations.entities import AnalystRatingChanges
from app.domains.coverage.recommendations.interfaces import RatingChangeAdapter
from app.domains.coverage.recommendations.interfaces import RatingChangesRepositoryAdapter

logger = logging.getLogger(__name__)


class RatingChangeAdapterImpl(RatingChangeAdapter):
    def __init__(
        self,
        inner: RatingChangeAdapter,
        repo: RatingChangesRepositoryAdapter,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A live
        # failure here has nothing to fall back on, so it propagates (→ 502).
        changes = self._inner.get_rating_changes(symbol)
        if not changes.is_empty:
            self._safe_upsert(symbol, changes)
        return changes

    def _safe_get(self, symbol: str) -> AnalystRatingChanges | None:
        # A cache read must never break the response: on any error, treat it as a miss and let
        # the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "rating-changes cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, changes: AnalystRatingChanges) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller already
        # has a good answer for. (Name comes from the sync job, not this feed, so it's left
        # untouched here — the insert-only upsert never clobbers a known name with None.)
        try:
            self._repo.upsert(symbol, None, changes)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "rating-changes cache write failed for %s", symbol, exc_info=True
            )
