"""Tests for the recommendations read endpoint (GET /stocks/{symbol}/recommendations).

Offline: a fake GetStockRecommendations is injected through dependency_overrides +
FastAPI's TestClient, so this checks only the controller + presenter — the JSON shape
(consensus, score, direction), the cache header, empty coverage as a 200 (not a 404), and
the error mapping — without touching Yahoo or the database.
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import recommendations_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRecommendations,
    RecommendationTrend,
)


class _FakeUseCase:
    """Stands in for GetStockRecommendations; returns a canned run or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> AnalystRecommendations:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_recommendations_use_case] = lambda: fake
    return TestClient(app)


def _a_trend(period, *, strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0):
    return RecommendationTrend(
        period=period,
        strong_buy=strong_buy,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
    )


def test_presents_the_run_with_consensus_and_direction():
    recs = AnalystRecommendations(
        "AAPL",
        (
            _a_trend(date(2026, 6, 1), strong_buy=13, buy=24, hold=7),  # mean 1.86
            _a_trend(date(2026, 5, 1), strong_buy=10, buy=20, hold=10, sell=1),  # 2.05
        ),
    )
    fake = _FakeUseCase(result=recs)
    resp = _client(fake).get("/stocks/AAPL/recommendations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    assert body["direction"] == "upgraded"  # consensus got more bullish MoM
    assert body["latest"]["consensus"] == "Buy"
    assert body["latest"]["score"] == 1.86
    assert body["latest"]["total"] == 44
    assert len(body["trends"]) == 2
    assert fake.calls == ["AAPL"]


def test_presents_the_price_targets_block():
    recs = AnalystRecommendations(
        "AAPL",
        (_a_trend(date(2026, 6, 1), buy=5),),
        price_targets=AnalystPriceTargets(mean=315.5, high=400.0, low=215.0, median=315.0),
    )
    body = _client(_FakeUseCase(result=recs)).get("/stocks/AAPL/recommendations").json()
    assert body["price_targets"] == {
        "mean": 315.5,
        "high": 400.0,
        "low": 215.0,
        "median": 315.0,
    }


def test_price_targets_is_null_when_absent():
    recs = AnalystRecommendations("AAPL", (_a_trend(date(2026, 6, 1), buy=5),))
    body = _client(_FakeUseCase(result=recs)).get("/stocks/AAPL/recommendations").json()
    assert body["price_targets"] is None


def test_sets_the_cache_header():
    fake = _FakeUseCase(
        result=AnalystRecommendations("AAPL", (_a_trend(date(2026, 6, 1), buy=5),))
    )
    resp = _client(fake).get("/stocks/AAPL/recommendations")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_no_trends():
    fake = _FakeUseCase(result=AnalystRecommendations("ZZZZ", ()))
    resp = _client(fake).get("/stocks/ZZZZ/recommendations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["latest"] is None
    assert body["direction"] is None
    assert body["trends"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/123/recommendations").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/recommendations").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/AAPL/recommendations").status_code == 502
