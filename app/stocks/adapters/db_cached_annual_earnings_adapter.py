import logging

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.annual.repository import AnnualEarningsRepository

logger = logging.getLogger(__name__)


class DbCachedAnnualEarningsProvider(AnnualEarningsProvider):
    def __init__(
        self,
        inner: AnnualEarningsProvider,
        repo: AnnualEarningsRepository,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A live
        # failure here has nothing to fall back on, so it propagates (→ 502).
        timeline = self._inner.get_annual_earnings(symbol)
        if not timeline.is_empty:
            self._safe_upsert(symbol, timeline)
        return timeline

    def _safe_get(self, symbol: str) -> AnnualEarningsTimeline | None:
        # A cache read must never break the (best-effort) earnings: on any error, treat it as
        # a miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "annual earnings cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, timeline: AnnualEarningsTimeline) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller already
        # has a good answer for. (Name comes from the sync job, not this feed, so it's left
        # untouched here.)
        try:
            self._repo.upsert(symbol, None, timeline)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "annual earnings cache write failed for %s", symbol, exc_info=True
            )
