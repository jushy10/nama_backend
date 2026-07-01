"""Interface Adapter: a read-through database cache in front of any QuarterlyEarningsProvider.

The read path calls the database first; only on a **miss** (no stored rows for the symbol)
does it hit Yahoo, store the result, and return it. A symbol that already has rows is always
served straight from the DB — the read never re-fetches based on age. Keeping stored rows
current is entirely the out-of-band cron's job (``SyncQuarterlyEarnings``), which rewrites a
stock's window on its schedule. This keeps the endpoint off Yahoo, which rate-limits and
blocks data-centre IPs.

It implements ``QuarterlyEarningsProvider``, so it slots into the wiring exactly where the
bare yfinance provider would, with the use case none the wiser.

Resilience:

- A cache *read* failure (DB hiccup) is treated as a miss, so a database problem falls
  through to the live source rather than sinking the (best-effort) earnings.
- A cache *write* failure is swallowed: the caller still gets the freshly-fetched timeline.
- An empty live result is not stored (there'd be nothing to store), so an uncovered symbol
  simply re-checks the live source on its next view rather than being cached as empty.
"""

import logging

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository

logger = logging.getLogger(__name__)


class DbCachedQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    """A read-through DB cache: serve stored rows, else fetch from the inner provider and store."""

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
