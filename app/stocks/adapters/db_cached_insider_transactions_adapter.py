"""Interface Adapter: a TTL read-through database cache in front of any InsiderTransactionsProvider.

The read path calls the database first; it serves the stored rows when they're **fresh** (their
newest fetch stamp is within the TTL) and otherwise re-fetches from SEC EDGAR, stores the result,
and returns it. This is the on-demand engine for the slice: unlike the earnings/segments caches
(no-TTL, kept current by an out-of-band cron), this slice has **no cron**, so the TTL is what
keeps a stock's feed from freezing after its first fetch — it self-refreshes on read once stale.
The precedent is the AI-analysis result cache (``SqlInvestmentAnalysisCache``), which likewise
uses a TTL rather than a cron.

It implements ``InsiderTransactionsProvider``, so it slots into the wiring exactly where the bare
SEC provider would, with the use case none the wiser.

Resilience — best-effort throughout, so a cache or source hiccup degrades rather than fails:

- A cache *read* failure (DB hiccup) is treated as a miss and falls through to the live source.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched activity.
- A **stale** cache is served when the live re-fetch fails or comes back empty — stale insider
  history beats erroring or blanking the feed. Only a **cold** miss (nothing stored) with a
  failing source propagates (→ 502).
- An empty live result is not stored (there'd be nothing to store), so a stock with no recent
  activity simply re-checks the live source on its next view rather than being cached as empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.repository import InsiderTransactionsRepository

logger = logging.getLogger(__name__)


class DbCachedInsiderTransactionsProvider(InsiderTransactionsProvider):
    """A TTL read-through DB cache: serve fresh stored rows, else re-fetch, store, and serve."""

    def __init__(
        self,
        inner: InsiderTransactionsProvider,
        repo: InsiderTransactionsRepository,
        *,
        ttl: timedelta,
        now=None,
    ) -> None:
        self._inner = inner
        self._repo = repo
        self._ttl = ttl
        # Injectable clock keeps freshness deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        stored = self._safe_get(symbol)
        if stored is not None and self._is_fresh(symbol):
            return stored  # a fresh cache is served straight from the DB

        # Cold miss or stale: re-fetch from the live source. A failure or an empty result falls
        # back to the stale cache when we have one — stale history beats erroring/blanking.
        try:
            activity = self._inner.get_insider_transactions(symbol)
        except (StockNotFound, StockDataUnavailable):
            if stored is not None:
                return stored
            raise  # cold miss with a failing source has nothing to fall back on (→ 502)

        if activity.is_empty:
            return stored if stored is not None else activity
        self._safe_upsert(symbol, activity)
        # Return the merged feed from the DB, not the raw live window. The live source only carries
        # its recent ~25-filing window, but the store accumulates a longer insert-only history; a
        # bare ``return activity`` would make the one read that trips the TTL serve a visibly
        # shorter list than the reads around it. Re-reading also yields the canonical serving order
        # so every read (fresh / refetched / cold) is byte-identical. Fall back to the live result
        # only if the write-then-read didn't land (best-effort cache).
        merged = self._safe_get(symbol)
        return merged if merged is not None else activity

    def _is_fresh(self, symbol: str) -> bool:
        """True when the symbol's newest stored fetch stamp is within the TTL. Any read error is
        treated as "not fresh" so the caller re-fetches rather than serving on a bad read."""
        try:
            stamp = self._repo.latest_fetched_at(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "insider transactions freshness read failed for %s", symbol, exc_info=True
            )
            return False
        if stamp is None:
            return False
        # SQLite drops tzinfo; normalize a naive stamp to UTC before comparing.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (self._now() - stamp) < self._ttl

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
        # a good answer for. (Name is the anchor's concern elsewhere, so it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, activity)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "insider transactions cache write failed for %s", symbol, exc_info=True
            )
