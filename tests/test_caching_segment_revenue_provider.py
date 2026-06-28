"""Unit tests for the segment-revenue TTL cache.

No real time and no real source: an injected clock drives expiry and a recording
inner provider counts upstream calls. Verifies the cache's contract — serve
within the window, refetch after it, isolate symbols, cache an empty result, and
never pin a failure. Mirrors the quarterly-revenue cache tests.
"""

from datetime import date

import pytest

from app.stocks.caching_segment_revenue_provider import CachingSegmentRevenueProvider
from app.stocks.entities import RevenueBreakdown, RevenueComponent
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import SegmentRevenueProvider


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingInner(SegmentRevenueProvider):
    """Pops one queued item per call (a dict is returned, an Exception raised);
    records every symbol it was asked for."""

    def __init__(self, queue):
        self._queue = list(queue)
        self.received: list[str] = []

    def get_quarterly_segment_revenue(self, symbol: str) -> dict:
        self.received.append(symbol)
        item = self._queue.pop(0) if self._queue else {}
        if isinstance(item, Exception):
            raise item
        return item


def _breakdown(amount: float) -> RevenueBreakdown:
    return RevenueBreakdown(by_segment=(RevenueComponent("AWS", amount),))


def cache_of(inner, ttl=100.0, clock=None):
    return CachingSegmentRevenueProvider(
        inner, ttl_seconds=ttl, clock=clock or FakeClock()
    )


def test_serves_from_cache_within_ttl():
    payload = {date(2026, 3, 31): _breakdown(97e9)}
    inner = RecordingInner([payload])
    cache = cache_of(inner)
    first = cache.get_quarterly_segment_revenue("AAPL")
    second = cache.get_quarterly_segment_revenue("AAPL")
    assert first == payload
    assert second == first
    assert inner.received == ["AAPL"]  # second hit served from cache


def test_refetches_after_ttl_expires():
    clock = FakeClock()
    inner = RecordingInner(
        [{date(2026, 3, 31): _breakdown(1.0)}, {date(2026, 3, 31): _breakdown(2.0)}]
    )
    cache = cache_of(inner, ttl=100.0, clock=clock)
    assert cache.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)] == _breakdown(1.0)
    clock.advance(101.0)  # past the TTL window
    assert cache.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)] == _breakdown(2.0)
    assert inner.received == ["AAPL", "AAPL"]


def test_caches_symbols_independently():
    inner = RecordingInner(
        [{date(2026, 3, 31): _breakdown(1.0)}, {date(2026, 3, 31): _breakdown(2.0)}]
    )
    cache = cache_of(inner)
    assert cache.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)] == _breakdown(1.0)
    assert cache.get_quarterly_segment_revenue("MSFT")[date(2026, 3, 31)] == _breakdown(2.0)
    assert cache.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)] == _breakdown(1.0)
    assert inner.received == ["AAPL", "MSFT"]


def test_caches_empty_result():
    # A symbol with no disclosed breakdown (empty map) is cached too — and this
    # one matters most: it spares the parser a re-scan of several full filings.
    inner = RecordingInner([{}])
    cache = cache_of(inner)
    assert cache.get_quarterly_segment_revenue("ZZZZ") == {}
    assert cache.get_quarterly_segment_revenue("ZZZZ") == {}
    assert inner.received == ["ZZZZ"]


def test_failure_is_not_cached():
    # A transient failure must propagate and retry next time, not be pinned.
    inner = RecordingInner(
        [StockDataUnavailable("AAPL", "boom"), {date(2026, 3, 31): _breakdown(5.0)}]
    )
    cache = cache_of(inner)
    with pytest.raises(StockDataUnavailable):
        cache.get_quarterly_segment_revenue("AAPL")
    assert cache.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)] == _breakdown(5.0)
    assert inner.received == ["AAPL", "AAPL"]
