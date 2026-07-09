"""Interface Adapter: a read-through database cache in front of any RevenueSegmentsProvider.

The read path calls the database first; only on a **miss** (no stored rows for the symbol) does
it hit SEC EDGAR, store the result, and return it. A symbol that already has rows is always
served straight from the DB — the read never re-fetches based on age. Keeping stored rows
current is entirely the out-of-band cron's job (``SyncRevenueSegments``), which merges each
stock's newest filing on its schedule. This keeps the endpoint off EDGAR — a multi-request
filing walk under a ~10 req/s ceiling — for all but the first view of a symbol.

It implements ``RevenueSegmentsProvider``, so it slots into the wiring exactly where the bare
SEC provider would, with the use case none the wiser.

Resilience:

- A cache *read* failure (DB hiccup) is treated as a miss, so a database problem falls through
  to the live source rather than sinking the (best-effort) segments.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched segmentation.
- An empty live result is not stored (there'd be nothing to store), so a company with no
  disaggregation simply re-checks the live source on its next view rather than being cached as
  empty.
"""

import logging

from app.stocks.revenue_segments.entities import RevenueSegmentation
from app.stocks.revenue_segments.ports import RevenueSegmentsProvider
from app.stocks.revenue_segments.repository import RevenueSegmentsRepository

logger = logging.getLogger(__name__)


class DbCachedRevenueSegmentsProvider(RevenueSegmentsProvider):
    """A read-through DB cache: serve stored rows, else fetch from the inner provider and store."""

    def __init__(
        self,
        inner: RevenueSegmentsProvider,
        repo: RevenueSegmentsRepository,
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
