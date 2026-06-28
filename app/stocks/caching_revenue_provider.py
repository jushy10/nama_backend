"""Interface Adapter: a TTL cache in front of any RevenueHistoryProvider.

Reported quarterly revenue only changes when a company files (roughly once a
quarter), but the earnings endpoint would otherwise hit the revenue source on
every request. This decorator collapses repeat lookups of the same symbol onto
one upstream call per TTL window — gentle on SEC EDGAR's ~10 req/s limit and
keeping the endpoint fast. It wraps any RevenueHistoryProvider, so the cache is
independent of which source backs it.

Only successful results are cached — including an empty map (a symbol the source
doesn't cover), so it isn't re-fetched every request. Failures propagate
uncached, so a transient outage retries on the next request rather than being
pinned for the whole TTL.
"""

import threading
import time
from datetime import date

from app.stocks.ports import RevenueHistoryProvider


class CachingRevenueHistoryProvider(RevenueHistoryProvider):
    """Wraps a RevenueHistoryProvider with a per-symbol, time-boxed cache."""

    _DEFAULT_TTL_SECONDS = 12 * 60 * 60  # half a day; filings are quarterly

    def __init__(
        self,
        inner: RevenueHistoryProvider,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[date, float]]] = {}

    def get_quarterly_revenue(self, symbol: str) -> dict[date, float]:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is not None and entry[0] > now:  # not yet expired
                return entry[1]
        # Fetch outside the lock so a slow upstream call doesn't block lookups of
        # other symbols. A concurrent miss on the same symbol may fetch twice —
        # benign (idempotent) and rare. A failure propagates without being cached.
        revenue = self._inner.get_quarterly_revenue(symbol)
        with self._lock:
            self._cache[symbol] = (now + self._ttl, revenue)
        return revenue
