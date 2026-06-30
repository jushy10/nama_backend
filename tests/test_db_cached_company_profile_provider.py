"""Tests for the DB-cache decorator on CompanyProfileProvider.

Offline and DB-free: a hand-written fake repository and fake inner provider stand in
for the real ones, so this exercises only the decorator's policy — when it serves the
cache, when it refreshes, how it treats an empty (vendor-miss) refresh, and how it
stays resilient to a cache or vendor failure — independent of SQLAlchemy.
"""

from datetime import datetime, timedelta, timezone

from app.stocks.db_cached_company_profile_provider import (
    DbCachedCompanyProfileProvider,
)
from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import (
    CachedProfile,
    CompanyProfileProvider,
    CompanyProfileRepository,
)

_NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)
_FRESH = _NOW - timedelta(days=30)
_STALE = _NOW - timedelta(days=200)  # past the 180-day default max age
_EMPTY = CompanyProfile(name=None, description=None)


def _profile(name: str) -> CompanyProfile:
    return CompanyProfile(name=name, description=f"{name} does things.")


class FakeRepo(CompanyProfileRepository):
    def __init__(self) -> None:
        self.rows: dict[str, CachedProfile] = {}
        self.upsert_calls = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol: str, profile: CompanyProfile, fetched_at: datetime) -> None:
        self.rows[symbol] = CachedProfile(profile, fetched_at)

    def get(self, symbol: str) -> CachedProfile | None:
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.rows.get(symbol)

    def upsert(self, symbol: str, profile: CompanyProfile) -> None:
        self.upsert_calls += 1
        if self.fail_upsert:
            raise RuntimeError("db write down")
        self.rows[symbol] = CachedProfile(profile, _NOW)


class FakeInner(CompanyProfileProvider):
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get_profile(self, symbol: str) -> CompanyProfile:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _decorator(inner: FakeInner, repo: FakeRepo) -> DbCachedCompanyProfileProvider:
    return DbCachedCompanyProfileProvider(inner, repo, now=lambda: _NOW)


def test_miss_fetches_from_inner_and_stores():
    inner = FakeInner(result=_profile("Apple"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"
    assert inner.calls == 1
    assert repo.upsert_calls == 1
    assert "AAPL" in repo.rows


def test_fresh_hit_serves_cache_without_calling_inner():
    inner = FakeInner(result=_profile("Other"))
    repo = FakeRepo()
    repo.preload("AAPL", _profile("Apple"), _FRESH)
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"
    assert inner.calls == 0


def test_stale_hit_refreshes_from_inner():
    inner = FakeInner(result=_profile("Apple v2"))
    repo = FakeRepo()
    repo.preload("AAPL", _profile("Apple"), _STALE)
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple v2"
    assert inner.calls == 1
    assert repo.rows["AAPL"].profile.name == "Apple v2"


def test_serves_stale_when_refresh_raises():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "vendor down"))
    repo = FakeRepo()
    repo.preload("AAPL", _profile("Apple"), _STALE)
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"  # the stale row, not an error
    assert inner.calls == 1


def test_empty_refresh_keeps_the_stale_row_and_does_not_cache_it():
    # The composite returns all-None rather than raising when both vendors miss; that
    # must not blank a known profile or get stored.
    inner = FakeInner(result=_EMPTY)
    repo = FakeRepo()
    repo.preload("AAPL", _profile("Apple"), _STALE)
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"  # kept the stale-but-real profile
    assert repo.upsert_calls == 0  # the empty result wasn't cached


def test_empty_refresh_with_nothing_stored_returns_empty_uncached():
    inner = FakeInner(result=_EMPTY)
    repo = FakeRepo()
    out = _decorator(inner, repo).get_profile("ZZZZ")
    assert out.name is None and out.description is None
    assert repo.upsert_calls == 0  # nothing worth caching


def test_miss_with_failing_inner_propagates():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "vendor down"))
    repo = FakeRepo()
    try:
        _decorator(inner, repo).get_profile("AAPL")
    except StockDataUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected StockDataUnavailable to propagate")


def test_cache_read_failure_falls_through_to_inner():
    inner = FakeInner(result=_profile("Apple"))
    repo = FakeRepo()
    repo.fail_get = True
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"  # served live instead of erroring
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_profile("Apple"))
    repo = FakeRepo()
    repo.fail_upsert = True
    out = _decorator(inner, repo).get_profile("AAPL")
    assert out.name == "Apple"  # caller still gets the fresh profile
    assert inner.calls == 1
