from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import PeHistory, PeHistoryPoint


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> PeHistory:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_pe_history_use_case] = lambda: fake
    return TestClient(app)


def _history() -> PeHistory:
    return PeHistory(
        symbol="AAPL",
        points=(
            PeHistoryPoint(
                report_date=date(2024, 2, 1),
                price=185.123,
                ttm_eps=6.4321,
                pe=28.78,
            ),
            PeHistoryPoint(
                report_date=date(2024, 5, 1),
                price=190.0,
                ttm_eps=6.5,
                pe=29.23,
            ),
        ),
    )


def test_presents_the_history_with_counts_and_rounding():
    fake = _FakeUseCase(result=_history())
    resp = _client(fake).get("/stocks/ticker/aapl/pe-history")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["count"] == 2
    assert body["points"][0] == {
        "date": "2024-02-01",
        "price": 185.12,  # rounded at the edge
        "ttm_eps": 6.43,
        "pe": 28.78,
    }
    assert body["stats"] is None  # only 2 points -> too thin to rank
    assert fake.calls == ["aapl"]


def _history_with_stats() -> PeHistory:
    # Enough points (>= MIN_POINTS_FOR_STATS) for the entity to publish a stats block; the last
    # point (the lowest multiple) reads as cheap vs the rest.
    pes = [20, 22, 24, 26, 28, 30, 25, 15]
    return PeHistory(
        symbol="AAPL",
        points=tuple(
            PeHistoryPoint(report_date=date(2022, 1, 1), price=100.0, ttm_eps=5.0, pe=float(pe))
            for pe in pes
        ),
    )


def test_presents_the_valuation_stats_block():
    resp = _client(_FakeUseCase(result=_history_with_stats())).get(
        "/stocks/ticker/AAPL/pe-history"
    )

    assert resp.status_code == 200
    stats = resp.json()["stats"]
    assert stats["signal"] == "cheap"
    assert stats["current_pe"] == 15.0
    assert stats["median_pe"] == 24.5
    assert stats["current_percentile"] < 25
    assert stats["sample_size"] == 8


def test_empty_history_is_a_200_not_a_404():
    resp = _client(_FakeUseCase(result=PeHistory(symbol="ZZZZ", points=()))).get(
        "/stocks/ticker/ZZZZ/pe-history"
    )
    assert resp.status_code == 200
    assert resp.json() == {"ticker": "ZZZZ", "count": 0, "points": [], "stats": None}


def test_bad_symbol_is_a_400():
    resp = _client(_FakeUseCase(error=ValueError("bad symbol"))).get(
        "/stocks/ticker/xx/pe-history"
    )
    assert resp.status_code == 400


def test_upstream_failure_is_a_502():
    resp = _client(_FakeUseCase(error=StockDataUnavailable("AAPL", "alpaca down"))).get(
        "/stocks/ticker/AAPL/pe-history"
    )
    assert resp.status_code == 502


def test_not_found_is_a_404():
    resp = _client(_FakeUseCase(error=StockNotFound("AAPL"))).get(
        "/stocks/ticker/AAPL/pe-history"
    )
    assert resp.status_code == 404


def test_sets_a_cache_header():
    resp = _client(_FakeUseCase(result=_history())).get("/stocks/ticker/AAPL/pe-history")
    assert "max-age" in resp.headers.get("Cache-Control", "")
