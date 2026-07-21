from datetime import date

from app.stocks.adapters.db_cached_revenue_segments_adapter import (
    DbCachedRevenueSegmentsProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)
from app.stocks.revenue_segments.ports import RevenueSegmentsProvider
from app.stocks.revenue_segments.repository import RevenueSegmentsRepository


def _seg(symbol: str, value: float) -> RevenueSegmentation:
    return RevenueSegmentation(
        symbol,
        (RevenueSegment(2024, date(2024, 12, 31), SegmentAxis.BUSINESS, "A", value),),
    )


def _empty(symbol: str) -> RevenueSegmentation:
    return RevenueSegmentation(symbol, ())


class FakeRepo(RevenueSegmentsRepository):
    def __init__(self) -> None:
        self.rows: dict[str, RevenueSegmentation] = {}
        self.upsert_calls = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol: str, seg: RevenueSegmentation) -> None:
        self.rows[symbol] = seg

    def get(self, symbol: str) -> RevenueSegmentation | None:
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.rows.get(symbol)

    def upsert(self, symbol, name, segmentation) -> None:
        self.upsert_calls += 1
        if self.fail_upsert:
            raise RuntimeError("db write down")
        self.rows[symbol] = segmentation

    def refresh_targets(self, limit):
        return []  # unused by the read-path decorator under test


class FakeInner(RevenueSegmentsProvider):
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _decorator(inner, repo) -> DbCachedRevenueSegmentsProvider:
    return DbCachedRevenueSegmentsProvider(inner, repo)


def test_stored_symbol_is_served_from_the_db_without_calling_inner():
    inner = FakeInner(result=_seg("GOOGL", 999))  # would differ if it were called
    repo = FakeRepo()
    repo.preload("GOOGL", _seg("GOOGL", 58.7e9))
    out = _decorator(inner, repo).get_revenue_segments("GOOGL")
    assert out.segments[0].value == 58.7e9  # the stored value, regardless of age
    assert inner.calls == 0 and repo.upsert_calls == 0


def test_miss_fetches_from_inner_and_stores():
    inner = FakeInner(result=_seg("GOOGL", 58.7e9))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_revenue_segments("GOOGL")
    assert out.segments[0].value == 58.7e9
    assert inner.calls == 1 and repo.upsert_calls == 1
    assert "GOOGL" in repo.rows  # cached for next time


def test_empty_live_result_is_returned_but_not_stored():
    inner = FakeInner(result=_empty("ZZZZ"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_revenue_segments("ZZZZ")
    assert out.is_empty
    assert repo.upsert_calls == 0  # nothing worth caching


def test_miss_with_failing_inner_propagates():
    inner = FakeInner(error=StockDataUnavailable("GOOGL", "sec down"))
    repo = FakeRepo()
    try:
        _decorator(inner, repo).get_revenue_segments("GOOGL")
    except StockDataUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected StockDataUnavailable to propagate")


def test_cache_read_failure_falls_through_to_inner():
    inner = FakeInner(result=_seg("GOOGL", 58.7e9))
    repo = FakeRepo()
    repo.fail_get = True
    out = _decorator(inner, repo).get_revenue_segments("GOOGL")
    assert out.segments[0].value == 58.7e9  # served live instead of erroring
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_seg("GOOGL", 58.7e9))
    repo = FakeRepo()
    repo.fail_upsert = True
    out = _decorator(inner, repo).get_revenue_segments("GOOGL")
    assert out.segments[0].value == 58.7e9  # caller still gets the fresh result
    assert inner.calls == 1
