"""Tests for the stock-search endpoint (GET /stocks/search).

Offline: a fake SearchStocks is injected through dependency_overrides, so this checks only
the controller — it passes q/limit through, presents the results (query echoed + counted),
and enforces the required, bounded query params — without touching the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import universe_search_endpoints as search
from app.stocks.universe.entities import ScreenedStock


class _FakeSearch:
    """Stands in for SearchStocks; records the (query, limit) it was called with."""

    def __init__(self, results=()) -> None:
        self._results = tuple(results)
        self.calls: list[tuple[str, int | None]] = []

    def execute(self, query, *, limit=None):
        self.calls.append((query, limit))
        return self._results


def _client(fake: _FakeSearch) -> TestClient:
    app = FastAPI()
    app.include_router(search.router)
    app.dependency_overrides[search.get_search_stocks] = lambda: fake
    return TestClient(app)


def test_returns_matches_with_the_query_echoed_and_counted():
    fake = _FakeSearch(
        (
            ScreenedStock(
                ticker="AAPL",
                name="Apple Inc.",
                exchange="NASDAQ",
                market_cap=3e12,
                sector="Technology",
            ),
            ScreenedStock(ticker="AMD", name="Advanced Micro Devices", market_cap=2e11),
        )
    )
    resp = _client(fake).get("/stocks/search?q=a&limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "a"
    assert body["count"] == 2
    assert body["results"][0] == {
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "market_cap": 3e12,
        "sector": "Technology",
    }
    assert fake.calls == [("a", 5)]  # q + limit reached the use case


def test_defaults_the_limit_when_omitted():
    fake = _FakeSearch(())
    resp = _client(fake).get("/stocks/search?q=xyz")
    assert resp.status_code == 200
    assert resp.json() == {"query": "xyz", "count": 0, "results": []}
    assert fake.calls == [("xyz", search.SearchStocks.DEFAULT_LIMIT)]


def test_requires_a_query():
    fake = _FakeSearch(())
    # q is required (min_length=1); omitting it fails validation before the use case runs.
    assert _client(fake).get("/stocks/search").status_code == 422
    assert fake.calls == []


def test_rejects_an_out_of_range_limit():
    fake = _FakeSearch(())
    assert _client(fake).get("/stocks/search?q=a&limit=0").status_code == 422
    assert _client(fake).get("/stocks/search?q=a&limit=1000").status_code == 422
    assert fake.calls == []
