from datetime import date

import pytest

from app.adapters.db.db_cached_recommendation_adapter_impl import (
    RecommendationAdapterImpl,
)
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.coverage.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)


def _a_run(symbol: str) -> AnalystRecommendations:
    return AnalystRecommendations(
        symbol,
        (
            RecommendationTrend(
                period=date(2026, 6, 1), strong_buy=13, buy=24, hold=7, sell=1, strong_sell=0
            ),
        ),
    )


class _FakeInner:
    def __init__(self, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
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

    def upsert(self, symbol, name, recommendations) -> None:
        if self._upsert_error is not None:
            raise self._upsert_error
        self.upserts.append((symbol, name))


def test_hit_serves_stored_rows_without_touching_the_live_source():
    stored = _a_run("AAPL")
    inner = _FakeInner()
    out = RecommendationAdapterImpl(inner, _FakeRepo(stored=stored)).get_recommendations("AAPL")
    assert out is stored
    assert inner.calls == []  # never went to Yahoo


def test_miss_fetches_once_stores_and_returns():
    live = _a_run("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(stored=None)
    out = RecommendationAdapterImpl(inner, repo).get_recommendations("AAPL")
    assert out is live
    assert inner.calls == ["AAPL"]
    assert repo.upserts == [("AAPL", None)]  # cached for the next read; no name from this feed


def test_empty_live_result_is_returned_but_not_cached():
    inner = _FakeInner(result=AnalystRecommendations("ZZZZ", ()))
    repo = _FakeRepo(stored=None)
    out = RecommendationAdapterImpl(inner, repo).get_recommendations("ZZZZ")
    assert out.is_empty
    assert repo.upserts == []  # nothing stored; the next view re-checks the live source


def test_cache_read_failure_degrades_to_a_miss():
    live = _a_run("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(get_error=RuntimeError("db down"))
    out = RecommendationAdapterImpl(inner, repo).get_recommendations("AAPL")
    assert out is live  # fell through to the live source


def test_cache_write_failure_never_sinks_the_response():
    live = _a_run("AAPL")
    inner = _FakeInner(result=live)
    repo = _FakeRepo(stored=None, upsert_error=RuntimeError("db down"))
    out = RecommendationAdapterImpl(inner, repo).get_recommendations("AAPL")
    assert out is live  # the caller still gets the fresh fetch


def test_live_failure_on_a_miss_propagates():
    inner = _FakeInner(error=StockDataUnavailable("AAPL", "blocked"))
    with pytest.raises(StockDataUnavailable):
        RecommendationAdapterImpl(inner, _FakeRepo(stored=None)).get_recommendations("AAPL")
