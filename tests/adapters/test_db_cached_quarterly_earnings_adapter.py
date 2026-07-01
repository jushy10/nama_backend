"""Tests for the DB-cache decorator on QuarterlyEarningsProvider.

Offline and DB-free: a hand-written fake repository (a dict) and fake inner provider stand
in for the real ones, so this exercises only the decorator's policy — when it serves the
cache, when it refreshes, how it stays resilient to a cache or vendor failure, and that a
transient *empty* live result never overwrites stored history — independent of SQLAlchemy.
"""

from datetime import date, datetime, timedelta, timezone

from app.stocks.adapters.db_cached_quarterly_earnings_adapter import (
    DbCachedQuarterlyEarningsProvider,
)
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import (
    CachedQuarterlyEarnings,
    QuarterlyEarningsRepository,
)
from app.stocks.exceptions import StockDataUnavailable

_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
_FRESH = _NOW - timedelta(days=1)
_STALE = _NOW - timedelta(days=10)  # past the 7-day default max age


def _tl(symbol: str, eps_actual: float) -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(
        symbol=symbol,
        quarters=(
            QuarterlyEarnings(
                fiscal_year=2025,
                fiscal_quarter=4,
                period_end=date(2025, 12, 31),
                report_date=date(2026, 2, 1),
                eps_actual=eps_actual,
                eps_estimate=3.0,
                eps_surprise=None,
                eps_surprise_percent=None,
                revenue_estimate=None,
            ),
        ),
    )


def _empty(symbol: str) -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(symbol, ())


class FakeRepo(QuarterlyEarningsRepository):
    def __init__(self) -> None:
        self.rows: dict[str, CachedQuarterlyEarnings] = {}
        self.get_calls = 0
        self.upsert_calls = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol, timeline, fetched_at) -> None:
        self.rows[symbol] = CachedQuarterlyEarnings(timeline, fetched_at)

    def get(self, symbol: str) -> CachedQuarterlyEarnings | None:
        self.get_calls += 1
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.rows.get(symbol)

    def upsert(self, symbol, name, timeline) -> None:
        self.upsert_calls += 1
        if self.fail_upsert:
            raise RuntimeError("db write down")
        self.rows[symbol] = CachedQuarterlyEarnings(timeline, _NOW)

    def refresh_targets(self, limit: int):
        return []  # unused by the read-path decorator under test


class FakeInner(QuarterlyEarningsProvider):
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _decorator(inner: FakeInner, repo: FakeRepo) -> DbCachedQuarterlyEarningsProvider:
    return DbCachedQuarterlyEarningsProvider(inner, repo, now=lambda: _NOW)


def test_miss_fetches_from_inner_and_stores():
    inner = FakeInner(result=_tl("AAPL", 3.3))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 3.3
    assert inner.calls == 1 and repo.upsert_calls == 1
    assert "AAPL" in repo.rows


def test_fresh_hit_serves_cache_without_calling_inner():
    inner = FakeInner(result=_tl("AAPL", 9.9))  # would differ if it were called
    repo = FakeRepo()
    repo.preload("AAPL", _tl("AAPL", 3.3), _FRESH)
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 3.3  # the cached value
    assert inner.calls == 0


def test_stale_hit_refreshes_from_inner():
    inner = FakeInner(result=_tl("AAPL", 4.0))
    repo = FakeRepo()
    repo.preload("AAPL", _tl("AAPL", 3.3), _STALE)
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 4.0  # refreshed
    assert inner.calls == 1
    assert repo.rows["AAPL"].timeline.quarters[0].eps_actual == 4.0


def test_serves_stale_when_refresh_fails():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "yahoo down"))
    repo = FakeRepo()
    repo.preload("AAPL", _tl("AAPL", 3.3), _STALE)
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 3.3  # the stale rows, rather than an error
    assert inner.calls == 1


def test_miss_with_failing_inner_propagates():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "yahoo down"))
    repo = FakeRepo()
    try:
        _decorator(inner, repo).get_quarterly_earnings("AAPL")
    except StockDataUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected StockDataUnavailable to propagate")


def test_empty_live_result_does_not_overwrite_cached_history():
    inner = FakeInner(result=_empty("AAPL"))  # transient empty from Yahoo
    repo = FakeRepo()
    repo.preload("AAPL", _tl("AAPL", 3.3), _STALE)
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert not out.is_empty and out.quarters[0].eps_actual == 3.3  # kept the cached rows
    assert repo.upsert_calls == 0  # the empty was never written


def test_empty_live_result_with_no_cache_returns_empty_unstored():
    inner = FakeInner(result=_empty("ZZZZ"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_quarterly_earnings("ZZZZ")
    assert out.is_empty
    assert repo.upsert_calls == 0


def test_cache_read_failure_falls_through_to_inner():
    inner = FakeInner(result=_tl("AAPL", 3.3))
    repo = FakeRepo()
    repo.fail_get = True
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 3.3  # served live instead of erroring
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_tl("AAPL", 3.3))
    repo = FakeRepo()
    repo.fail_upsert = True
    out = _decorator(inner, repo).get_quarterly_earnings("AAPL")
    assert out.quarters[0].eps_actual == 3.3  # caller still gets the fresh timeline
    assert inner.calls == 1
