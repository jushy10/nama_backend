"""Interface Adapter: a TTL cache in front of any SegmentRevenueProvider.

The segment breakdown is the most expensive enrichment on the earnings endpoint
— it fetches and parses several full inline-XBRL filings per symbol — yet it only
changes when the company files (roughly quarterly). This decorator collapses
repeat lookups of the same symbol onto one upstream pass per TTL window, which
matters far more here than for the flat revenue source: it's the difference
between one cheap fetch and re-parsing a handful of multi-megabyte filings on
every request. It wraps any SegmentRevenueProvider, so the cache is independent
of which source backs it.

Only successful results are cached — including an empty map (a symbol with no
disclosed breakdown), so it isn't re-fetched every request. Failures propagate
uncached, so a transient outage retries next request rather than being pinned
for the whole TTL. Mirrors ``CachingRevenueHistoryProvider``.
"""

import threading
import time
from datetime import date

from app.stocks.entities import RevenueBreakdown
from app.stocks.ports import SegmentRevenueProvider


class CachingSegmentRevenueProvider(SegmentRevenueProvider):
    """Wraps a SegmentRevenueProvider with a per-symbol, time-boxed cache."""

    _DEFAULT_TTL_SECONDS = 12 * 60 * 60  # half a day; filings are quarterly

    def __init__(
        self,
        inner: SegmentRevenueProvider,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[date, RevenueBreakdown]]] = {}

    def get_quarterly_segment_revenue(
        self, symbol: str
    ) -> dict[date, RevenueBreakdown]:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is not None and entry[0] > now:  # not yet expired
                return entry[1]
        # Fetch outside the lock so a slow upstream pass doesn't block lookups of
        # other symbols. A concurrent miss on the same symbol may fetch twice —
        # benign (idempotent) and rare. A failure propagates without being cached.
        breakdowns = self._inner.get_quarterly_segment_revenue(symbol)
        with self._lock:
            self._cache[symbol] = (now + self._ttl, breakdowns)
        return breakdowns
