"""Tests for the ticker read endpoint (GET /stocks/ticker/{symbol}).

Offline: a fake GetTickerCard is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape (symbol
renamed to ``ticker``, the day move, the enrichment blocks, the ``metrics.forward_peg``
figure, the ``1w``/``1m`` performance aliases), the cache header, missing enrichment as
nulls (not a 404), and the error mapping — without touching Alpaca, Finnhub, or the
database.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.entities import Quote, StockFundamentals, StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.use_cases import TickerCard


class _FakeUseCase:
    """Stands in for GetTickerCard; returns a canned card or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> TickerCard:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_card_use_case] = lambda: fake
    return TestClient(app)


def _a_card(*, with_enrichment: bool = True, forward_peg_legs=(13.3, 104.1)) -> TickerCard:
    forward_pe, forward_eps_growth = forward_peg_legs
    return TickerCard(
        quote=Quote(
            symbol="MU",
            price=975.56,
            previous_close=963.26,
            bid=None,
            ask=None,
            as_of=datetime(2026, 7, 3, tzinfo=timezone.utc),
        ),
        valuation=TickerValuation(
            symbol="MU",
            price=975.56,
            forward_pe=forward_pe,
            forward_eps_growth=forward_eps_growth,
        ),
        fundamentals=(
            StockFundamentals(
                market_cap=1_090_000_000_000.0,
                dividend_per_share=0.46,
                dividend_yield=0.05,
            )
            if with_enrichment
            else None
        ),
        performance=(
            StockPerformance(
                one_week=1.5, one_month=8.0, three_month=40.0, six_month=90.0,
                ytd=120.0, one_year=150.0,
            )
            if with_enrichment
            else None
        ),
    )


def test_presents_the_full_card():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "MU"  # the symbol, in this endpoint's vocabulary
    assert body["price"] == 975.56
    assert body["change"] == 12.3  # vs the previous close, same rule as /quote
    assert body["change_percent"] == 1.28
    assert body["market_cap"] == 1_090_000_000_000.0
    assert body["dividend_per_share"] == 0.46
    assert body["dividend_yield"] == 0.05
    # Performance keeps the finance-style aliases the snapshot uses.
    assert body["performance"] == {
        "1w": 1.5, "1m": 8.0, "3m": 40.0, "6m": 90.0, "ytd": 120.0, "1y": 150.0,
    }
    assert body["metrics"] == {"forward_peg": 0.13}
    assert fake.calls == ["MU"]


def test_sets_the_cache_header():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_missing_enrichment_and_coverage_is_a_200_with_nulls():
    fake = _FakeUseCase(
        result=_a_card(with_enrichment=False, forward_peg_legs=(None, None))
    )
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == 975.56
    assert body["market_cap"] is None
    assert body["dividend_per_share"] is None
    assert body["dividend_yield"] is None
    assert body["performance"] is None
    assert body["metrics"] == {"forward_peg": None}


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("MU", "boom"))
    assert _client(fake).get("/stocks/ticker/MU").status_code == 502
