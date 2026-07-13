"""Interface Adapter: a read-through database cache in front of any InsiderTransactionsProvider.

The read path calls the database first; only on a **miss** (no stored rows for the symbol) does
it hit SEC EDGAR, store the result, and return it. A symbol that already has rows is always
served straight from the DB — the read never re-fetches based on age. Keeping stored rows current
is entirely the out-of-band cron's job (``SyncInsiderTransactions``), which renews each stock's
Form 4 feed on its weekly schedule. This keeps the endpoint off EDGAR — a multi-request filing
walk under a ~10 req/s ceiling — for all but the first view of a symbol.

This mirrors the sibling cache slices (revenue-segments / news / recommendations) exactly. The
slice previously used a TTL-on-read cache with no cron; adding the weekly sweep let it drop the
TTL so a synced stock never triggers the live walk inside a user request — the perf fix.

It implements ``InsiderTransactionsProvider``, so it slots into the wiring exactly where the bare
SEC provider would, with the use case none the wiser.

Resilience:

- A cache *read* failure (DB hiccup) is treated as a miss, so a database problem falls through to
  the live source rather than sinking the (best-effort) feed.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched activity.
- An empty live result is not stored (there'd be nothing to store), so a stock with no recent
  activity simply re-checks the live source on its next view rather than being cached as empty.
"""

from __future__ import annotations

import logging

from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.repository import InsiderTransactionsRepository

logger = logging.getLogger(__name__)


class DbCachedInsiderTransactionsProvider(InsiderTransactionsProvider):
    """A read-through DB cache: serve stored rows, else fetch from the inner provider and store."""

    def __init__(
        self,
        inner: InsiderTransactionsProvider,
        repo: InsiderTransactionsRepository,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A live
        # failure here has nothing to fall back on, so it propagates (→ 502).
        activity = self._inner.get_insider_transactions(symbol)
        if not activity.is_empty:
            self._safe_upsert(symbol, activity)
        return activity

    def _safe_get(self, symbol: str) -> InsiderActivity | None:
        # A cache read must never break the (best-effort) feed: on any error, treat it as a miss
        # and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "insider transactions cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, activity: InsiderActivity) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller already has
        # a good answer for. (Name comes from the sync job, not this feed, so it's left untouched
        # here.)
        try:
            self._repo.upsert(symbol, None, activity)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "insider transactions cache write failed for %s", symbol, exc_info=True
            )
