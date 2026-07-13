"""Offline tests for the stock-performance sync use case.

``SyncStockPerformance`` is driven through hand-written fakes for its two ports — the batched
performance feed (``BulkPerformanceProvider``) and the persistence repository — so nothing
touches Alpaca or SQLAlchemy. The batched-feed shape (one call for the whole work-list) is the
key difference from the per-stock earnings/fundamentals sweeps, so the tests focus on: the
single fetch over the work-list, the stale-first targets passing through, the skipped count for
targets the feed returned no history for, and the swallowed total-outage.
"""

from __future__ import annotations

from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.performance.repository import PerformanceRepository
from app.stocks.performance.use_cases import SyncStockPerformance


def _perf(one_year=None, **windows):
    return StockPerformance(
        one_week=windows.get("one_week"),
        one_month=windows.get("one_month"),
        three_month=windows.get("three_month"),
        six_month=windows.get("six_month"),
        ytd=windows.get("ytd"),
        one_year=one_year,
    )


class FakeRepo(PerformanceRepository):
    """Serves a fixed stale-first work-list and records what was written."""

    def __init__(self, targets):
        self._targets = tuple(targets)
        self.written: dict[str, StockPerformance] | None = None

    def refresh_targets(self, limit):
        self.limit = limit
        return self._targets if limit is None else self._targets[:limit]

    def set_performance(self, performance_by_ticker):
        self.written = dict(performance_by_ticker)
        return len(self.written)


class FakeBulkPerformance:
    def __init__(self, performance=None, error=None):
        self._performance = performance or {}
        self._error = error
        self.requested: tuple[str, ...] | None = None

    def get_bulk_performance(self, symbols):
        self.requested = tuple(symbols)
        if self._error is not None:
            raise self._error
        return dict(self._performance)


def test_execute_fetches_the_worklist_in_one_call_and_writes_it():
    repo = FakeRepo(["NVDA", "JPM"])
    feed = FakeBulkPerformance({"NVDA": _perf(one_year=120.0), "JPM": _perf(one_year=15.0)})

    report = SyncStockPerformance(feed, repo).execute()

    assert feed.requested == ("NVDA", "JPM")  # one batched call over the whole work-list
    assert set(repo.written) == {"NVDA", "JPM"}
    assert repo.written["NVDA"].one_year == 120.0
    assert report.refreshed == 2
    assert report.skipped == 0
    assert report.limit is None


def test_execute_counts_targets_the_feed_returned_no_history_for_as_skipped():
    repo = FakeRepo(["NVDA", "NEWLY", "JPM"])
    # NEWLY has too little history -> absent from the feed's map.
    feed = FakeBulkPerformance({"NVDA": _perf(one_year=120.0), "JPM": _perf(one_year=15.0)})

    report = SyncStockPerformance(feed, repo).execute()

    assert set(repo.written) == {"NVDA", "JPM"}  # NEWLY not written -> left un-stamped
    assert report.refreshed == 2
    assert report.skipped == 1  # NEWLY


def test_execute_passes_the_limit_through_to_the_worklist():
    repo = FakeRepo(["A", "B", "C", "D"])
    feed = FakeBulkPerformance({"A": _perf(one_year=1.0), "B": _perf(one_year=2.0)})

    report = SyncStockPerformance(feed, repo).execute(limit=2)

    assert repo.limit == 2
    assert feed.requested == ("A", "B")  # only the capped work-list is fetched
    assert report.refreshed == 2
    assert report.limit == 2


def test_execute_empty_worklist_makes_no_feed_call():
    repo = FakeRepo([])
    feed = FakeBulkPerformance({"X": _perf(one_year=1.0)})

    report = SyncStockPerformance(feed, repo).execute()

    assert feed.requested is None  # nothing to fetch
    assert repo.written is None  # nothing to write
    assert report == type(report)(refreshed=0, skipped=0, limit=None)


def test_execute_total_feed_outage_is_swallowed_and_writes_nothing():
    repo = FakeRepo(["NVDA", "JPM"])
    feed = FakeBulkPerformance(error=StockDataUnavailable("performance", "every chunk failed"))

    report = SyncStockPerformance(feed, repo).execute()

    # A whole-batch outage leaves the anchor untouched (best-effort colour) and counts every
    # target as skipped so the next sweep retries it — never a raised error.
    assert repo.written is None
    assert report.refreshed == 0
    assert report.skipped == 2
