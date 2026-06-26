"""Unit tests for the company-profile TTL cache.

No real time and no real vendor: an injected clock drives expiry and a recording
inner provider counts upstream calls. Verifies the cache's contract — serve
within the window, refetch after it, isolate symbols, cache "no description", and
never pin a failure.
"""

import pytest

from app.stocks.caching_company_profile_provider import CachingCompanyProfileProvider
from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import CompanyProfileProvider


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingInner(CompanyProfileProvider):
    """Pops one queued item per call (a CompanyProfile is returned, an Exception
    raised); records every symbol it was asked for."""

    def __init__(self, queue):
        self._queue = list(queue)
        self.received: list[str] = []

    def get_profile(self, symbol: str) -> CompanyProfile:
        self.received.append(symbol)
        item = self._queue.pop(0) if self._queue else CompanyProfile(None)
        if isinstance(item, Exception):
            raise item
        return item


def cache_of(inner, ttl=100.0, clock=None):
    return CachingCompanyProfileProvider(inner, ttl_seconds=ttl, clock=clock or FakeClock())


def test_serves_from_cache_within_ttl():
    inner = RecordingInner([CompanyProfile("Apple makes phones.")])
    cache = cache_of(inner)
    first = cache.get_profile("AAPL")
    second = cache.get_profile("AAPL")
    assert first.description == "Apple makes phones."
    assert second == first
    assert inner.received == ["AAPL"]  # second hit served from cache


def test_refetches_after_ttl_expires():
    clock = FakeClock()
    inner = RecordingInner([CompanyProfile("v1"), CompanyProfile("v2")])
    cache = cache_of(inner, ttl=100.0, clock=clock)
    assert cache.get_profile("AAPL").description == "v1"
    clock.advance(101.0)  # past the TTL window
    assert cache.get_profile("AAPL").description == "v2"
    assert inner.received == ["AAPL", "AAPL"]


def test_caches_symbols_independently():
    inner = RecordingInner([CompanyProfile("A"), CompanyProfile("M")])
    cache = cache_of(inner)
    assert cache.get_profile("AAPL").description == "A"
    assert cache.get_profile("MSFT").description == "M"
    assert cache.get_profile("AAPL").description == "A"  # still cached
    assert inner.received == ["AAPL", "MSFT"]


def test_caches_absent_description():
    # An uncovered symbol (no description) is cached too, so it isn't re-fetched.
    inner = RecordingInner([CompanyProfile(None)])
    cache = cache_of(inner)
    assert cache.get_profile("ZZZZ").description is None
    assert cache.get_profile("ZZZZ").description is None
    assert inner.received == ["ZZZZ"]


def test_failure_is_not_cached():
    # A transient failure must propagate and retry next time, not be pinned.
    inner = RecordingInner(
        [StockDataUnavailable("AAPL", "boom"), CompanyProfile("recovered")]
    )
    cache = cache_of(inner)
    with pytest.raises(StockDataUnavailable):
        cache.get_profile("AAPL")
    assert cache.get_profile("AAPL").description == "recovered"
    assert inner.received == ["AAPL", "AAPL"]
