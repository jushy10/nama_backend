from datetime import date

import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)
from app.stocks.company.revenue_segments.interfaces import RevenueSegmentsAdapter
from app.stocks.company.revenue_segments.interfaces import (
    RefreshTarget,
    RevenueSegmentsRepositoryAdapter,
)
from app.stocks.company.revenue_segments.use_cases import (
    GetRevenueSegments,
    RevenueSegmentsSyncReport,
    SyncRevenueSegments,
)


def _segmentation(symbol: str) -> RevenueSegmentation:
    return RevenueSegmentation(
        symbol=symbol,
        segments=(
            RevenueSegment(2024, date(2024, 12, 31), SegmentAxis.BUSINESS, "A", 100e9),
        ),
    )


class _FakeReadProvider(RevenueSegmentsAdapter):
    def __init__(self, result: RevenueSegmentation) -> None:
        self._result = result
        self.calls: list[str] = []

    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        self.calls.append(symbol)
        return self._result


def test_get_normalizes_the_symbol_before_calling_the_provider():
    result = RevenueSegmentation("AAPL", ())
    provider = _FakeReadProvider(result)
    out = GetRevenueSegments(provider).execute("  aapl ")
    assert out is result
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_a_blank_symbol():
    provider = _FakeReadProvider(RevenueSegmentation("", ()))
    with pytest.raises(ValueError):
        GetRevenueSegments(provider).execute("   ")
    assert provider.calls == []


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(RevenueSegmentation("", ()))
    for bad in ("123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetRevenueSegments(provider).execute(bad)
    assert provider.calls == []


class _FakeRepo(RevenueSegmentsRepositoryAdapter):
    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.saved: dict[str, RevenueSegmentation] = {}
        self.refresh_limit: int | None = "unset"

    def get(self, symbol: str) -> RevenueSegmentation | None:
        return None

    def upsert(self, symbol, name, segmentation) -> None:
        self.upserts.append((symbol, name))
        self.saved[symbol] = segmentation

    def refresh_targets(self, limit) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets if limit is None else self._targets[:limit]


class _FakeSyncProvider(RevenueSegmentsAdapter):
    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return RevenueSegmentation(symbol, ())
        return _segmentation(symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo([RefreshTarget("GOOGL", "Alphabet"), RefreshTarget("MSFT", None)])
    provider = _FakeSyncProvider()

    report = SyncRevenueSegments(provider, repo).execute(limit=10)

    assert isinstance(report, RevenueSegmentsSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["GOOGL", "MSFT"]  # serial, in stalest order
    assert repo.upserts == [("GOOGL", "Alphabet"), ("MSFT", None)]  # name carried through


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("GOOGL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(errors={"BAD": StockDataUnavailable("BAD", "sec down")})

    report = SyncRevenueSegments(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["GOOGL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncRevenueSegments(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    repo = _FakeRepo([RefreshTarget("GOOGL", "Alphabet"), RefreshTarget("GONE", None)])
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncRevenueSegments(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("GOOGL", "Alphabet")]  # GONE never upserted


def test_sync_defaults_to_unlimited_when_no_limit_is_given():
    repo = _FakeRepo([])
    SyncRevenueSegments(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)


def test_sync_limit_is_passed_through_and_floored_at_one():
    repo = _FakeRepo([])
    SyncRevenueSegments(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncRevenueSegments(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
