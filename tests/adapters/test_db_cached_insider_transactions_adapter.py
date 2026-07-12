"""Tests for the TTL read-through DB cache on InsiderTransactionsProvider.

Offline and DB-free: a hand-written fake repository (which mirrors the real repo's insert-only
merge) and fake inner provider stand in for the real ones, so this exercises only the decorator's
policy — serve a *fresh* cache, re-fetch when stale or cold, return the *merged accumulated feed*
(not the short live window) after a re-fetch, serve *stale* rows on a live failure or empty
result, don't cache an empty result, and stay resilient to a cache read or write failure —
independent of SQLAlchemy and the clock.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.stocks.adapters.db_cached_insider_transactions_adapter import (
    DbCachedInsiderTransactionsProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.repository import InsiderTransactionsRepository

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
_TTL = timedelta(hours=24)


def _txn(key: str) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=date(2026, 6, 17),
        transaction_date=date(2026, 6, 15),
        insider_name="Jane",
        officer_title="CEO",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        security_title="Common Stock",
        transaction_code="P",
        acquired_disposed="A",
        shares=100.0,
        price_per_share=1.0,
        shares_owned_following=None,
        accession_number=key,
        line_index=0,
    )


def _activity(symbol: str, *keys: str) -> InsiderActivity:
    return InsiderActivity(symbol, tuple(_txn(k) for k in keys))


def _keys(activity: InsiderActivity) -> set[str]:
    return {t.accession_number for t in activity.transactions}


class FakeRepo(InsiderTransactionsRepository):
    """Mirrors the real repo's contract: insert-only merge by (accession, line_index) on upsert,
    and a fetch stamp refreshed on every upsert."""

    def __init__(self) -> None:
        self.activity: dict[str, InsiderActivity] = {}
        self.stamp: dict[str, datetime] = {}
        self.upserts = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol: str, activity: InsiderActivity, stamp: datetime) -> None:
        self.activity[symbol] = activity
        self.stamp[symbol] = stamp

    def get(self, symbol: str) -> InsiderActivity | None:
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.activity.get(symbol)

    def latest_fetched_at(self, symbol: str) -> datetime | None:
        return self.stamp.get(symbol)

    def upsert(self, symbol, name, activity) -> None:
        self.upserts += 1
        if self.fail_upsert:
            raise RuntimeError("db write down")
        existing = self.activity.get(symbol)
        if existing is None:
            merged = activity.transactions
        else:
            seen = {(t.accession_number, t.line_index) for t in existing.transactions}
            new = tuple(
                t
                for t in activity.transactions
                if (t.accession_number, t.line_index) not in seen
            )
            merged = existing.transactions + new  # insert-only accumulation
        self.activity[symbol] = InsiderActivity(symbol, merged)
        self.stamp[symbol] = _NOW


class FakeInner(InsiderTransactionsProvider):
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _decorator(inner, repo) -> DbCachedInsiderTransactionsProvider:
    return DbCachedInsiderTransactionsProvider(inner, repo, ttl=_TTL, now=lambda: _NOW)


def test_fresh_cache_is_served_without_calling_inner():
    inner = FakeInner(result=_activity("AAPL", "z"))  # would differ if it were called
    repo = FakeRepo()
    repo.preload("AAPL", _activity("AAPL", "a", "b"), _NOW - timedelta(hours=1))
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a", "b"}
    assert inner.calls == 0 and repo.upserts == 0


def test_stale_refetch_returns_the_merged_feed_not_the_short_live_window():
    # Store has a long accumulated history; the live source only carries its recent window. After a
    # stale re-fetch the decorator must return the *merged* feed, not the short live window — else
    # the one read that trips the TTL would serve a visibly shorter list than the reads around it.
    inner = FakeInner(result=_activity("AAPL", "a", "b", "new"))  # short window (+1 new key)
    repo = FakeRepo()
    repo.preload(
        "AAPL", _activity("AAPL", "a", "b", "c", "d", "e"), _NOW - timedelta(hours=48)
    )
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a", "b", "c", "d", "e", "new"}  # full merged feed, incl. the new one
    assert inner.calls == 1 and repo.upserts == 1


def test_cold_miss_fetches_stores_and_returns():
    inner = FakeInner(result=_activity("AAPL", "a", "b"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a", "b"}
    assert inner.calls == 1 and repo.upserts == 1


def test_fresh_via_a_naive_stamp():
    # SQLite drops tzinfo, so latest_fetched_at can return a naive datetime; the decorator must
    # normalize it to UTC before comparing (else a TypeError). A naive stamp within the TTL is fresh.
    inner = FakeInner(result=_activity("AAPL", "z"))
    repo = FakeRepo()
    naive_recent = (_NOW - timedelta(hours=1)).replace(tzinfo=None)
    repo.preload("AAPL", _activity("AAPL", "a"), naive_recent)
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"} and inner.calls == 0  # served fresh from the naive-stamped cache


def test_exactly_at_the_ttl_boundary_is_stale():
    # Freshness is a strict `<` comparison, so a stamp exactly one TTL old is stale -> re-fetch.
    inner = FakeInner(result=_activity("AAPL", "a"))
    repo = FakeRepo()
    repo.preload("AAPL", _activity("AAPL", "a"), _NOW - _TTL)
    _decorator(inner, repo).get_insider_transactions("AAPL")
    assert inner.calls == 1  # boundary counts as stale


def test_live_failure_with_stale_cache_serves_stale():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "sec down"))
    repo = FakeRepo()
    repo.preload("AAPL", _activity("AAPL", "a"), _NOW - timedelta(hours=48))
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"}  # stale rows beat erroring
    assert inner.calls == 1 and repo.upserts == 0


def test_cold_miss_live_failure_propagates():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "sec down"))
    repo = FakeRepo()
    with pytest.raises(StockDataUnavailable):
        _decorator(inner, repo).get_insider_transactions("AAPL")


def test_empty_live_with_stale_cache_serves_stale():
    inner = FakeInner(result=InsiderActivity("AAPL"))  # empty live
    repo = FakeRepo()
    repo.preload("AAPL", _activity("AAPL", "a"), _NOW - timedelta(hours=48))
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"}  # empty result must not blank a populated cache
    assert repo.upserts == 0


def test_empty_live_on_cold_miss_returns_empty_and_is_not_stored():
    inner = FakeInner(result=InsiderActivity("ZZZZ"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_insider_transactions("ZZZZ")
    assert out.is_empty
    assert repo.upserts == 0  # nothing worth caching


def test_cache_read_failure_falls_through_to_inner():
    inner = FakeInner(result=_activity("AAPL", "a"))
    repo = FakeRepo()
    repo.fail_get = True
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"}  # served live instead of erroring (re-read also misses -> live)
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_activity("AAPL", "a"))
    repo = FakeRepo()
    repo.fail_upsert = True
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"}  # caller still gets the fresh result (re-read misses -> live)
    assert inner.calls == 1
