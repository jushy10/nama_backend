import logging

from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.company.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.company.earnings.quarterly.repository import QuarterlyEarningsRepository

logger = logging.getLogger(__name__)


class DbCachedQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    def __init__(
        self,
        inner: QuarterlyEarningsProvider,
        repo: QuarterlyEarningsRepository,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A
        # live failure here has nothing to fall back on, so it propagates (→ 502).
        timeline = self._inner.get_quarterly_earnings(symbol)
        if not timeline.is_empty:
            self._safe_upsert(symbol, timeline)
        return timeline

    def _safe_get(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        # A cache read must never break the (best-effort) earnings: on any error, treat it
        # as a miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "quarterly earnings cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, timeline: QuarterlyEarningsTimeline) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller
        # already has a good answer for. (Name comes from the sync job, not this feed, so
        # it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, timeline)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "quarterly earnings cache write failed for %s", symbol, exc_info=True
            )
