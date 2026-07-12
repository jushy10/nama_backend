"""Interface Adapter: a TTL cache in front of any BulkPerformanceProvider.

The heat map's trailing-window returns (1W…1Y, YTD) are, per request, the slice's
heaviest read by far: a year of daily bars for a whole index (~500 names for the
S&P 500), paginated and fetched from Alpaca. Yet those windows barely move
intra-session — they're trailing figures off split-adjusted daily closes. This
decorator collapses repeat boards of the same index onto one upstream fetch per
TTL window, so a burst of viewers (and the every-minute cache-miss the endpoint's
short ``Cache-Control`` lets through) doesn't re-download the index each time.

The cache is keyed by the *symbol set* (order/dupes/case-insensitive), so the two
index boards each get their own entry and a universe change naturally misses. It
wraps any ``BulkPerformanceProvider``, so the cache is independent of which feed
backs it.

Only successful results are cached — the whole returned map, including its
per-symbol omissions (a name with no history stays absent and isn't re-requested
within the window). A hard feed failure propagates *uncached*, so a transient
outage retries on the next request rather than being pinned for the whole TTL —
the same stance the company-profile cache takes (see
``CachingCompanyProfileProvider``). The use case already treats that failure as
best-effort (blank windows), so caching only ever makes the board faster.
"""

import threading
import time
from collections.abc import Sequence

from app.stocks.entities import StockPerformance
from app.stocks.ports import BulkPerformanceProvider


class CachingBulkPerformanceProvider(BulkPerformanceProvider):
    """Wraps a BulkPerformanceProvider with a per-symbol-set, time-boxed cache."""

    # The trailing windows are stable over hours; a few minutes bounds the bars
    # fetches to a handful per index per hour while keeping the board's longer
    # timeframes effectively current. The day-change tiles are a *separate*,
    # uncached leg, so the board's "today" colour is unaffected by this window.
    _DEFAULT_TTL_SECONDS = 5 * 60

    def __init__(
        self,
        inner: BulkPerformanceProvider,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key: the sorted, de-duped, upper-cased symbol tuple -> (expires_at, result)
        self._cache: dict[
            tuple[str, ...], tuple[float, dict[str, StockPerformance]]
        ] = {}

    def get_bulk_performance(
        self, symbols: Sequence[str]
    ) -> dict[str, StockPerformance]:
        key = tuple(sorted({s.upper() for s in symbols if s}))
        if not key:
            return {}
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry[0] > now:  # not yet expired
                return dict(entry[1])  # copy so a caller can't mutate the cached map
        # Fetch outside the lock so a slow upstream call doesn't block other lookups.
        # A concurrent miss on the same key may fetch twice — benign (idempotent) and
        # rare. A hard feed failure propagates without being cached.
        result = self._inner.get_bulk_performance(key)
        with self._lock:
            self._cache[key] = (now + self._ttl, dict(result))
        return dict(result)
