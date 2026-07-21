from datetime import date

import pytest

from app.stocks.adapters.db.db_cached_institutional_holders_adapter import (
    DbCachedInstitutionalOwnershipProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    InstitutionalHolder,
    InstitutionalOwnership,
)


def _ownership(symbol: str) -> InstitutionalOwnership:
    return InstitutionalOwnership(
        symbol=symbol,
        holders=(
            InstitutionalHolder(
                holder="Vanguard Group Inc",
                holder_type=HOLDER_TYPE_INSTITUTION,
                date_reported=date(2026, 6, 30),
                shares=1000.0,
                value=100000.0,
                pct_held=8.9,
                pct_change=1.0,
            ),
        ),
    )


class _FakeInner:
    def __init__(self, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeRepo:
    def __init__(self, stored=None, get_error=None, upsert_error=None) -> None:
        self._stored = stored
        self._get_error = get_error
        self._upsert_error = upsert_error
        self.upserts: list[tuple[str, str | None]] = []

    def get(self, symbol: str):
        if self._get_error is not None:
            raise self._get_error
        return self._stored

    def upsert(self, symbol, name, ownership) -> None:
        if self._upsert_error is not None:
            raise self._upsert_error
        self.upserts.append((symbol, name))


def test_hit_serves_stored_rows_without_touching_the_live_source():
    stored = _ownership("AAPL")
    inner = _FakeInner()
    out = DbCachedInstitutionalOwnershipProvider(
        inner, _FakeRepo(stored=stored)
    ).get_institutional_ownership("AAPL")
    assert out is stored
    assert inner.calls == []  # never went to Yahoo


def test_miss_fetches_once_stores_and_returns():
    live = _ownership("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(stored=None)
    out = DbCachedInstitutionalOwnershipProvider(inner, repo).get_institutional_ownership("AAPL")
    assert out is live
    assert inner.calls == ["AAPL"]
    assert repo.upserts == [("AAPL", None)]  # cached for the next read; no name from this feed


def test_empty_live_result_is_returned_but_not_cached():
    inner = _FakeInner(result=InstitutionalOwnership(symbol="ZZZZ"))
    repo = _FakeRepo(stored=None)
    out = DbCachedInstitutionalOwnershipProvider(inner, repo).get_institutional_ownership("ZZZZ")
    assert out.is_empty
    assert repo.upserts == []  # nothing stored; the next view re-checks the live source


def test_cache_read_failure_degrades_to_a_miss():
    live = _ownership("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(get_error=RuntimeError("db down"))
    out = DbCachedInstitutionalOwnershipProvider(inner, repo).get_institutional_ownership("AAPL")
    assert out is live  # fell through to the live source


def test_cache_write_failure_never_sinks_the_response():
    live = _ownership("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(stored=None, upsert_error=RuntimeError("db down"))
    out = DbCachedInstitutionalOwnershipProvider(inner, repo).get_institutional_ownership("AAPL")
    assert out is live  # the caller still gets the fresh fetch


def test_live_failure_on_a_miss_propagates():
    inner = _FakeInner(error=StockDataUnavailable("AAPL", "blocked"))
    with pytest.raises(StockDataUnavailable):
        DbCachedInstitutionalOwnershipProvider(inner, _FakeRepo(stored=None)).get_institutional_ownership("AAPL")
