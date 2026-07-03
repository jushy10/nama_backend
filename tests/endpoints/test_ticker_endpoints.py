"""Tests for the ticker read endpoint (GET /stocks/ticker/{symbol}).

Offline: a fake GetTickerValuation is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape (symbol
renamed to ``ticker``, the derived forward PEG as a plain field), the cache header,
missing coverage as a 200 with a null PEG (not a 404), and the error mapping — without
touching Alpaca or the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import TickerValuation


class _FakeUseCase:
    """Stands in for GetTickerValuation; returns a canned valuation or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> TickerValuation:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_valuation_use_case] = lambda: fake
    return TestClient(app)


def _a_valuation(**overrides) -> TickerValuation:
    defaults = dict(
        symbol="MU",
        price=975.56,
        forward_pe=13.3,
        forward_eps_growth=104.1,
    )
    return TickerValuation(**{**defaults, **overrides})


def test_presents_the_ticker_price_and_derived_forward_peg():
    fake = _FakeUseCase(result=_a_valuation())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The symbol is served as `ticker`; the legs stay snapshot-only, so the body
    # is exactly the ticker, the price the ratio embeds, and the PEG itself.
    assert body == {"ticker": "MU", "price": 975.56, "forward_peg": 0.13}
    assert fake.calls == ["MU"]


def test_sets_the_cache_header():
    fake = _FakeUseCase(result=_a_valuation())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_missing_coverage_is_a_200_with_a_null_peg():
    fake = _FakeUseCase(
        result=_a_valuation(forward_pe=None, forward_eps_growth=None)
    )
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == 975.56
    assert body["forward_peg"] is None


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("MU", "boom"))
    assert _client(fake).get("/stocks/ticker/MU").status_code == 502
