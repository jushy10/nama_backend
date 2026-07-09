"""Interface Adapter: a read-through database cache in front of any RatingChangeProvider.

The rating-changes analogue of ``db_cached_recommendations_adapter``. The read path calls
the database first; only on a **miss** (no stored events for the symbol) does it hit Yahoo,
store the result, and return it. A symbol that already has events is served straight from the
DB — the read never re-fetches based on age. Keeping stored events current is otherwise the
out-of-band cron's job (``SyncRecommendations`` folds the rating-change refresh into its
sweep), which adds newly-published events on its schedule. This keeps the endpoint off Yahoo,
which rate-limits and blocks data-centre IPs.

It implements ``RatingChangeProvider``, so it slots into the wiring exactly where the bare
yfinance provider would, with the use case none the wiser.

Resilience (identical to the recommendations cache):

- A cache *read* failure (DB hiccup) is treated as a miss, so a database problem falls
  through to the live source rather than sinking the response.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched events.
- An empty live result is not stored (there'd be nothing to store), so a symbol with no
  published actions simply re-checks the live source on its next view rather than being
  cached as empty.
"""

import logging

from app.stocks.recommendations.entities import AnalystRatingChanges
from app.stocks.recommendations.ports import RatingChangeProvider
from app.stocks.recommendations.repository import RatingChangesRepository

logger = logging.getLogger(__name__)


class DbCachedRatingChangeProvider(RatingChangeProvider):
    """A read-through DB cache: serve stored events, else fetch from the inner provider and store."""

    def __init__(
        self,
        inner: RatingChangeProvider,
        repo: RatingChangesRepository,
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
