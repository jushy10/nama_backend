import logging

from app.stocks.company.revenue_segments.entities import RevenueSegmentation
from app.stocks.company.revenue_segments.interfaces import RevenueSegmentsAdapter
from app.stocks.company.revenue_segments.interfaces import RevenueSegmentsRepositoryAdapter

logger = logging.getLogger(__name__)


class RevenueSegmentsAdapterImpl(RevenueSegmentsAdapter):
    def __init__(
        self,
        inner: RevenueSegmentsAdapter,
        repo: RevenueSegmentsRepositoryAdapter,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A live
        # failure here has nothing to fall back on, so it propagates (→ 502).
        segmentation = self._inner.get_revenue_segments(symbol)
        if not segmentation.is_empty:
            self._safe_upsert(symbol, segmentation)
        return segmentation

    def _safe_get(self, symbol: str) -> RevenueSegmentation | None:
        # A cache read must never break the (best-effort) segments: on any error, treat it as a
        # miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "revenue segments cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, segmentation: RevenueSegmentation) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller already
        # has a good answer for. (Name comes from the sync job, not this feed, so it's left
        # untouched here.)
        try:
            self._repo.upsert(symbol, None, segmentation)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "revenue segments cache write failed for %s", symbol, exc_info=True
            )
