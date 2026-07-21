import pytest

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.index_membership.entities import IndexMembershipSnapshot
from app.stocks.catalog.index_membership.repository import IndexMembershipSyncCounts
from app.stocks.catalog.index_membership.use_cases import SyncIndexMembership


def _tickers(prefix: str, n: int) -> frozenset[str]:
    return frozenset(f"{prefix}{i}" for i in range(n))


class FakeSource:
    def __init__(self, snapshot=None, *, error=None) -> None:
        self._snapshot = snapshot
        self._error = error
        self.calls = 0

    def fetch(self) -> IndexMembershipSnapshot:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


class FakeRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, bool]] = []

    def reconcile(self, snapshot, *, sync_sp500, sync_nasdaq100) -> IndexMembershipSyncCounts:
        self.calls.append((sync_sp500, sync_nasdaq100))
        return IndexMembershipSyncCounts(
            sp500_marked=len(snapshot.sp500) if sync_sp500 else 0,
            sp500_cleared=0,
            nasdaq100_marked=len(snapshot.nasdaq100) if sync_nasdaq100 else 0,
            nasdaq100_cleared=0,
        )


def test_healthy_lists_reconcile_both_indices():
    snap = IndexMembershipSnapshot(
        sp500=_tickers("S", SyncIndexMembership.MIN_PLAUSIBLE_SP500),
        nasdaq100=_tickers("N", SyncIndexMembership.MIN_PLAUSIBLE_NASDAQ100),
    )
    repo = FakeRepo()

    report = SyncIndexMembership(FakeSource(snap), repo).execute()

    assert repo.calls == [(True, True)]
    assert report.sp500_skipped is False
    assert report.nasdaq100_skipped is False
    assert report.sp500_members == SyncIndexMembership.MIN_PLAUSIBLE_SP500
    assert report.sp500_marked == SyncIndexMembership.MIN_PLAUSIBLE_SP500


def test_a_short_sp500_list_is_skipped():
    snap = IndexMembershipSnapshot(
        sp500=_tickers("S", 10),  # far below the floor — a truncated/blocked scrape
        nasdaq100=_tickers("N", SyncIndexMembership.MIN_PLAUSIBLE_NASDAQ100),
    )
    repo = FakeRepo()

    report = SyncIndexMembership(FakeSource(snap), repo).execute()

    # S&P is skipped (not reconciled); the healthy Nasdaq list still is.
    assert repo.calls == [(False, True)]
    assert report.sp500_skipped is True
    assert report.sp500_marked == 0
    assert report.nasdaq100_skipped is False


def test_a_short_nasdaq_list_is_skipped():
    snap = IndexMembershipSnapshot(
        sp500=_tickers("S", SyncIndexMembership.MIN_PLAUSIBLE_SP500),
        nasdaq100=_tickers("N", 5),  # below the floor
    )
    repo = FakeRepo()

    report = SyncIndexMembership(FakeSource(snap), repo).execute()

    assert repo.calls == [(True, False)]
    assert report.nasdaq100_skipped is True
    assert report.nasdaq100_marked == 0
    assert report.sp500_skipped is False


def test_a_hard_source_failure_propagates():
    source = FakeSource(error=StockDataUnavailable("*", "finnhub down"))
    repo = FakeRepo()

    with pytest.raises(StockDataUnavailable):
        SyncIndexMembership(source, repo).execute()

    assert repo.calls == []  # nothing reconciled when the fetch failed outright
