"""Interface Adapter: a TTL cache in front of any CompanyProfileProvider.

A company's business description is near-static — it changes maybe once a year —
but GET /stocks/{symbol} would otherwise call the profile vendor on every hit,
and FMP's free tier allows only ~250 calls/day. This decorator collapses repeat
lookups of the same symbol onto one upstream call per TTL window, keeping the
endpoint within quota. It wraps any CompanyProfileProvider, so the cache is
independent of which vendor backs it.

Only successful results are cached — including a "no description" result, so an
uncovered symbol isn't re-fetched every request. Failures propagate uncached, so
a transient outage retries on the next request rather than being pinned for the
whole TTL.
"""

import threading
import time

from app.stocks.entities import CompanyProfile
from app.stocks.ports import CompanyProfileProvider


class CachingCompanyProfileProvider(CompanyProfileProvider):
    """Wraps a CompanyProfileProvider with a per-symbol, time-boxed cache."""

    _DEFAULT_TTL_SECONDS = 24 * 60 * 60  # a day; descriptions rarely change

    def __init__(
        self,
        inner: CompanyProfileProvider,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, CompanyProfile]] = {}

    def get_profile(self, symbol: str) -> CompanyProfile:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is not None and entry[0] > now:  # not yet expired
                return entry[1]
        # Fetch outside the lock so a slow upstream call doesn't block lookups of
        # other symbols. A concurrent miss on the same symbol may fetch twice —
        # benign (idempotent) and rare. A failure propagates without being cached.
        profile = self._inner.get_profile(symbol)
        with self._lock:
            self._cache[symbol] = (now + self._ttl, profile)
        return profile
