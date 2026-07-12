"""Unit tests for the bulk-performance TTL cache.

No real time and no real vendor: an injected clock drives expiry and a recording
inner provider counts upstream calls. Verifies the cache's contract — serve within
the window, refetch after it, key by the symbol set (order/case/dupes don't
matter), cache the map's per-symbol omissions, and never pin a failure.
"""

import pytest

from app.stocks.caching_bulk_performance_provider import CachingBulkPerformanceProvider
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import BulkPerformanceProvider


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _perf(one_year=None):
    return StockPerformance(
        one_week=None,
        one_month=None,
        three_month=None,
        six_month=None,
        ytd=None,
        one_year=one_year,
    )


class RecordingInner(BulkPerformanceProvider):
    """Pops one queued item per call (a dict is returned, an Exception raised);
    records every symbol tuple it was asked for."""

    def __init__(self, queue):
        self._queue = list(queue)
        self.received: list[tuple[str, ...]] = []

    def get_bulk_performance(self, symbols):
        self.received.append(tuple(symbols))
        item = self._queue.pop(0) if self._queue else {}
        if isinstance(item, Exception):
            raise item
        return dict(item)


def cache_of(inner, ttl=100.0, clock=None):
    return CachingBulkPerformanceProvider(inner, ttl_seconds=ttl, clock=clock or FakeClock())


def test_serves_from_cache_within_ttl():
    inner = RecordingInner([{"NVDA": _perf(120.0)}])
    cache = cache_of(inner)
    first = cache.get_bulk_performance(["NVDA"])
    second = cache.get_bulk_performance(["NVDA"])
    assert first["NVDA"].one_year == 120.0
    assert second == first
    assert inner.received == [("NVDA",)]  # second hit served from cache


def test_refetches_after_ttl_expires():
    clock = FakeClock()
    inner = RecordingInner([{"NVDA": _perf(1.0)}, {"NVDA": _perf(2.0)}])
    cache = cache_of(inner, ttl=100.0, clock=clock)
    assert cache.get_bulk_performance(["NVDA"])["NVDA"].one_year == 1.0
    clock.advance(101.0)  # past the TTL window
    assert cache.get_bulk_performance(["NVDA"])["NVDA"].one_year == 2.0
    assert inner.received == [("NVDA",), ("NVDA",)]


def test_keys_by_symbol_set_ignoring_order_case_and_dupes():
    inner = RecordingInner([{"NVDA": _perf(1.0), "JPM": _perf(2.0)}])
    cache = cache_of(inner)
    first = cache.get_bulk_performance(["NVDA", "JPM"])
    # A differently-ordered, mixed-case, duplicated request is the same key -> a cache hit.
    second = cache.get_bulk_performance(["jpm", "nvda", "NVDA"])
    assert second == first
    assert inner.received == [("JPM", "NVDA")]  # sorted, de-duped, upper-cased once


def test_different_symbol_sets_are_cached_independently():
    inner = RecordingInner([{"NVDA": _perf(1.0)}, {"AAPL": _perf(2.0)}])
    cache = cache_of(inner)
    assert cache.get_bulk_performance(["NVDA"])["NVDA"].one_year == 1.0
    assert cache.get_bulk_performance(["AAPL"])["AAPL"].one_year == 2.0
    assert cache.get_bulk_performance(["NVDA"])["NVDA"].one_year == 1.0  # still cached
    assert inner.received == [("NVDA",), ("AAPL",)]


def test_caches_per_symbol_omissions():
    # A name the feed has no history for is absent from the map; the whole map (omissions and
    # all) is cached, so that name isn't re-requested within the window.
    inner = RecordingInner([{"NVDA": _perf(1.0)}])  # JPM omitted -> no history
    cache = cache_of(inner)
    first = cache.get_bulk_performance(["NVDA", "JPM"])
    second = cache.get_bulk_performance(["NVDA", "JPM"])
    assert "JPM" not in first
    assert second == first
    assert inner.received == [("JPM", "NVDA")]  # one fetch only


def test_empty_input_never_calls_upstream():
    inner = RecordingInner([{"NVDA": _perf(1.0)}])
    cache = cache_of(inner)
    assert cache.get_bulk_performance([]) == {}
    assert inner.received == []  # no symbols -> no upstream call


def test_failure_is_not_cached():
    # A hard feed failure must propagate and retry next time, not be pinned for the TTL.
    inner = RecordingInner(
        [StockDataUnavailable("performance", "boom"), {"NVDA": _perf(9.0)}]
    )
    cache = cache_of(inner)
    with pytest.raises(StockDataUnavailable):
        cache.get_bulk_performance(["NVDA"])
    assert cache.get_bulk_performance(["NVDA"])["NVDA"].one_year == 9.0
    assert inner.received == [("NVDA",), ("NVDA",)]


def test_returned_map_is_a_copy_callers_cannot_poison_the_cache():
    inner = RecordingInner([{"NVDA": _perf(1.0)}])
    cache = cache_of(inner)
    first = cache.get_bulk_performance(["NVDA"])
    first["JPM"] = _perf(999.0)  # mutate the returned map
    second = cache.get_bulk_performance(["NVDA"])
    assert "JPM" not in second  # the cache is untouched
