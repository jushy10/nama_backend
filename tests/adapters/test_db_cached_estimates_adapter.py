"""Tests for the DB-cache decorator on AnalystEstimatesProvider.

Offline and DB-free: a hand-written fake repository (a dict) and fake inner provider
stand in for the real ones, so this exercises only the decorator's policy — when it
serves the cache, when it refreshes, and how it stays resilient to a cache or vendor
failure — independent of SQLAlchemy.
"""

from datetime import date, datetime, timedelta, timezone

from app.stocks.adapters.db_cached_estimates_adapter import (
    DbCachedAnalystEstimatesProvider,
)
from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.estimates.ports import AnalystEstimatesProvider
from app.stocks.estimates.repository import AnalystEstimatesRepository, CachedEstimates

_NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)
_FRESH = _NOW - timedelta(days=1)
_STALE = _NOW - timedelta(days=40)  # past the 35-day default max age


def _est(eps_avg: float) -> AnalystEstimates:
    return AnalystEstimates(
        fiscal_year=2026, period_end=date(2026, 9, 30), eps_avg=eps_avg, eps_low=None,
        eps_high=None, revenue_avg=400e9, num_analysts_eps=10, num_analysts_revenue=10,
    )


class FakeRepo(AnalystEstimatesRepository):
    def __init__(self) -> None:
        self.rows: dict[str, CachedEstimates] = {}
        self.get_calls = 0
        self.upsert_calls = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol: str, est: AnalystEstimates, fetched_at: datetime) -> None:
        self.rows[symbol] = CachedEstimates(est, fetched_at)

    def get(self, symbol: str) -> CachedEstimates | None:
        self.get_calls += 1
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.rows.get(symbol)

    def upsert(self, symbol: str, name, est: AnalystEstimates) -> None:
        self.upsert_calls += 1
        if self.fail_upsert:
            raise RuntimeError("db write down")
        self.rows[symbol] = CachedEstimates(est, _NOW)

    def refresh_targets(self, limit: int):
        # Unused by the read-path decorator under test; present only to satisfy the
        # port (the sync use case's own tests cover refresh_targets).
        return []


class FakeInner(AnalystEstimatesProvider):
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _decorator(inner: FakeInner, repo: FakeRepo) -> DbCachedAnalystEstimatesProvider:
    return DbCachedAnalystEstimatesProvider(inner, repo, now=lambda: _NOW)


def test_miss_fetches_from_inner_and_stores():
    inner = FakeInner(result=_est(8.0))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 8.0
    assert inner.calls == 1
    assert repo.upsert_calls == 1
    assert "AAPL" in repo.rows


def test_fresh_hit_serves_cache_without_calling_inner():
    inner = FakeInner(result=_est(9.9))  # would differ if it were called
    repo = FakeRepo()
    repo.preload("AAPL", _est(8.0), _FRESH)
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 8.0  # the cached value
    assert inner.calls == 0


def test_stale_hit_refreshes_from_inner():
    inner = FakeInner(result=_est(9.0))
    repo = FakeRepo()
    repo.preload("AAPL", _est(8.0), _STALE)
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 9.0  # refreshed
    assert inner.calls == 1
    assert repo.rows["AAPL"].estimates.eps_avg == 9.0


def test_serves_stale_when_refresh_fails():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "Yahoo down"))
    repo = FakeRepo()
    repo.preload("AAPL", _est(8.0), _STALE)
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 8.0  # the stale row, rather than an error
    assert inner.calls == 1


def test_miss_with_failing_inner_propagates():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "Yahoo down"))
    repo = FakeRepo()
    try:
        _decorator(inner, repo).get_estimates("AAPL")
    except StockDataUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected StockDataUnavailable to propagate")


def test_cache_read_failure_falls_through_to_inner():
    inner = FakeInner(result=_est(8.0))
    repo = FakeRepo()
    repo.fail_get = True  # DB read blows up
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 8.0  # served from the live source instead of erroring
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_est(8.0))
    repo = FakeRepo()
    repo.fail_upsert = True  # DB write blows up
    out = _decorator(inner, repo).get_estimates("AAPL")
    assert out.eps_avg == 8.0  # caller still gets the fresh estimate
    assert inner.calls == 1
