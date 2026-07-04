"""Tests for the universe use cases: SyncUniverse + SearchStocks.

Offline: hand-written fakes for the screener and repository ports, so this exercises only
the orchestration — the upsert-vs-skip decision and count pass-through on the sync side,
query normalization and the limit cap on the search side — independent of Yahoo or the DB.
"""

import pytest

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.ports import StockScreener
from app.stocks.universe.repository import UniverseRepository, UniverseSyncCounts
from app.stocks.universe.use_cases import (
    SearchStocks,
    SyncUniverse,
    UniverseSyncReport,
)


def _stock(ticker, *, market_cap=1e10, name=None, exchange=None, sector=None):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
    )


def _a_screen(n: int) -> tuple[ScreenedStock, ...]:
    """A plausible screen of ``n`` distinct names, each above the floor."""
    return tuple(_stock(f"T{i:04d}", market_cap=5e9 + i) for i in range(n))


class _FakeScreener(StockScreener):
    """Returns a canned screen, or raises the given error."""

    def __init__(self, stocks=(), *, error=None) -> None:
        self._stocks = tuple(stocks)
        self._error = error
        self.calls: list[float] = []

    def screen(self, *, min_market_cap):
        self.calls.append(min_market_cap)
        if self._error is not None:
            raise self._error
        return self._stocks


class _FakeRepo(UniverseRepository):
    """Records the upsert input and returns canned counts; serves canned search hits."""

    def __init__(self, *, counts=UniverseSyncCounts(0, 0), hits=()) -> None:
        self._counts = counts
        self._hits = tuple(hits)
        self.upserted: tuple[ScreenedStock, ...] | None = None
        self.searches: list[tuple[str, int]] = []

    def upsert_screen(self, stocks):
        self.upserted = tuple(stocks)
        return self._counts

    def search(self, query, *, limit):
        self.searches.append((query, limit))
        return self._hits


# ───────────────────────────── SyncUniverse ─────────────────────────────


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=UniverseSyncCounts(added=3, updated=7))

    report = SyncUniverse(screener, repo).execute()

    assert isinstance(report, UniverseSyncReport)
    assert screener.calls == [SyncUniverse.MIN_MARKET_CAP]  # the floor is passed through
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 3, 7)
    assert report.skipped is False


def test_sync_skips_an_empty_screen_without_touching_the_store():
    screener = _FakeScreener(())
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    screener = _FakeScreener(_a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN - 1))
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo).execute()

    assert report.skipped is True
    assert repo.upserted is None


def test_sync_propagates_a_hard_screen_failure():
    screener = _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked"))
    repo = _FakeRepo()

    with pytest.raises(StockDataUnavailable):
        SyncUniverse(screener, repo).execute()
    assert repo.upserted is None  # nothing written on a hard failure


# ───────────────────────────── SearchStocks ─────────────────────────────


def test_search_normalizes_the_query_and_defaults_the_limit():
    hit = _stock("AAPL", name="Apple Inc.")
    repo = _FakeRepo(hits=(hit,))

    out = SearchStocks(repo).execute("  apple ")

    assert out == (hit,)
    assert repo.searches == [("apple", SearchStocks.DEFAULT_LIMIT)]


def test_search_rejects_a_blank_query():
    repo = _FakeRepo()
    with pytest.raises(ValueError):
        SearchStocks(repo).execute("   ")
    assert repo.searches == []  # rejected before the repo is touched


def test_search_caps_the_limit_and_floors_it_at_one():
    repo = _FakeRepo()
    SearchStocks(repo).execute("x", limit=10_000)
    SearchStocks(repo).execute("x", limit=0)
    assert repo.searches == [("x", SearchStocks.MAX_LIMIT), ("x", 1)]
