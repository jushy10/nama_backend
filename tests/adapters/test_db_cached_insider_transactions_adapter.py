"""Tests for the read-through DB cache on InsiderTransactionsProvider.

Offline and DB-free: a hand-written fake repository (which mirrors the real repo's insert-only
merge) and fake inner provider stand in for the real ones, so this exercises only the decorator's
policy — serve stored rows straight from the DB at any age, fetch from the live source only on a
cold miss (nothing stored), don't cache an empty result, propagate a cold-miss live failure, and
stay resilient to a cache read or write failure — independent of SQLAlchemy.

No TTL: the slice moved from a TTL-on-read cache (self-refreshing per read) to a plain
read-through kept warm by the weekly sync cron, so a populated symbol is never re-fetched inside a
read. Freshness is the sweep's job (covered in the use-case + repository tests), not the cache's.
"""

from datetime import date

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
from app.stocks.insider_transactions.repository import (
    InsiderTransactionsRepository,
    RefreshTarget,
)


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
    """Mirrors the real repo's contract: insert-only merge by (accession, line_index) on upsert."""

    def __init__(self) -> None:
        self.activity: dict[str, InsiderActivity] = {}
        self.upserts = 0
        self.fail_get = False
        self.fail_upsert = False

    def preload(self, symbol: str, activity: InsiderActivity) -> None:
        self.activity[symbol] = activity

    def get(self, symbol: str) -> InsiderActivity | None:
        if self.fail_get:
            raise RuntimeError("db read down")
        return self.activity.get(symbol)

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

    def refresh_targets(self, limit) -> list[RefreshTarget]:  # unused by the read cache
        return []


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
    return DbCachedInsiderTransactionsProvider(inner, repo)


def test_stored_is_served_without_calling_inner():
    inner = FakeInner(result=_activity("AAPL", "z"))  # would differ if it were called
    repo = FakeRepo()
    repo.preload("AAPL", _activity("AAPL", "a", "b"))
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a", "b"}  # the stored feed, any age
    assert inner.calls == 0 and repo.upserts == 0


def test_cold_miss_fetches_stores_and_returns():
    inner = FakeInner(result=_activity("AAPL", "a", "b"))
    repo = FakeRepo()
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a", "b"}
    assert inner.calls == 1 and repo.upserts == 1


def test_cold_miss_live_failure_propagates():
    inner = FakeInner(error=StockDataUnavailable("AAPL", "sec down"))
    repo = FakeRepo()
    with pytest.raises(StockDataUnavailable):
        _decorator(inner, repo).get_insider_transactions("AAPL")


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
    assert _keys(out) == {"a"}  # served live instead of erroring on a bad read
    assert inner.calls == 1


def test_cache_write_failure_does_not_break_the_response():
    inner = FakeInner(result=_activity("AAPL", "a"))
    repo = FakeRepo()
    repo.fail_upsert = True
    out = _decorator(inner, repo).get_insider_transactions("AAPL")
    assert _keys(out) == {"a"}  # caller still gets the fresh result
    assert inner.calls == 1
