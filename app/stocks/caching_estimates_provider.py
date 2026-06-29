"""Interface Adapter: a TTL cache in front of any AnalystEstimatesProvider.

Forward analyst estimates move slowly — analysts revise a handful of times a
quarter — but GET /stocks/{symbol} would otherwise call the estimates vendor on
every hit, and FMP's free tier allows only ~250 calls/day. This decorator collapses
repeat lookups of the same symbol onto one upstream call per TTL window, keeping the
endpoint within quota. It wraps any AnalystEstimatesProvider, so the cache is
independent of which vendor backs it.

Only successful results are cached — including an empty "no estimates" result, so an
uncovered symbol isn't re-fetched every request. Failures propagate uncached, so a
transient outage retries on the next request rather than being pinned for the whole
TTL. The same shape as the company-profile and revenue cache decorators.
"""

import threading
import time

from app.stocks.entities import AnalystEstimates
from app.stocks.ports import AnalystEstimatesProvider


class CachingAnalystEstimatesProvider(AnalystEstimatesProvider):
    """Wraps an AnalystEstimatesProvider with a per-symbol, time-boxed cache."""

    _DEFAULT_TTL_SECONDS = 12 * 60 * 60  # half a day; estimates revise slowly

    def __init__(
        self,
        inner: AnalystEstimatesProvider,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, AnalystEstimates]] = {}

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is not None and entry[0] > now:  # not yet expired
                return entry[1]
        # Fetch outside the lock so a slow upstream call doesn't block lookups of
        # other symbols. A concurrent miss on the same symbol may fetch twice —
        # benign (idempotent) and rare. A failure propagates without being cached.
        estimates = self._inner.get_estimates(symbol)
        with self._lock:
            self._cache[symbol] = (now + self._ttl, estimates)
        return estimates
