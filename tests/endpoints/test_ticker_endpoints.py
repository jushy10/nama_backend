"""Tests for the ticker read endpoint (GET /stocks/ticker/{ticker}).

Offline: a fake GetTickerCard is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape (symbol
renamed to ``ticker``, the day move, the opt-in ``dividend``/``performance``/``metrics``
blocks with the ``1w``/``1m`` performance aliases), the include pass-through, the cache
header, unrequested/unavailable blocks as nulls (not a 404), and the error mapping —
without touching Alpaca, Finnhub, or the database.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.entities import (
    CompanyProfile,
    Quote,
    StockFundamentals,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.use_cases import TickerCard


class _FakeUseCase:
    """Stands in for GetTickerCard; returns a canned card or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, list[str] | None]] = []

    def execute(self, symbol: str, include=None) -> TickerCard:
        self.calls.append((symbol, include))
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_card_use_case] = lambda: fake
    return TestClient(app)


def _a_card(*, include: frozenset[str] = frozenset()) -> TickerCard:
    """A canned card; the opt-in blocks are populated only when in ``include``,
    the way the use case builds it."""
    return TickerCard(
        quote=Quote(
            symbol="MU",
            price=975.56,
            previous_close=963.26,
            bid=None,
            ask=None,
            as_of=datetime(2026, 7, 3, tzinfo=timezone.utc),
        ),
        include=include,
        valuation=(
            TickerValuation(
                symbol="MU", price=975.56, forward_pe=13.3, forward_eps_growth=104.1
            )
            if "metrics" in include
            else None
        ),
        profile=CompanyProfile(name="Micron Technology"),
        fundamentals=StockFundamentals(
            market_cap=1_090_000_000_000.0,
            dividend_per_share=0.46,
            dividend_yield=0.05,
        ),
        performance=(
            StockPerformance(
                one_week=1.5, one_month=8.0, three_month=40.0, six_month=90.0,
                ytd=120.0, one_year=150.0,
            )
            if "performance" in include
            else None
        ),
    )


def test_presents_the_core_card_with_null_optin_blocks_by_default():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "MU"  # the symbol, in this endpoint's vocabulary
    assert body["name"] == "Micron Technology"  # profile vendor's clean display name
    assert body["price"] == 975.56
    assert body["change"] == 12.3  # vs the previous close, same rule as /quote
    assert body["change_percent"] == 1.28
    assert body["market_cap"] == 1_090_000_000_000.0
    # Opt-in blocks stay null until requested — even though the fundamentals
    # (which carry the dividend) were fetched for the market cap.
    assert body["dividend"] is None
    assert body["performance"] is None
    assert body["metrics"] is None
    assert fake.calls == [("MU", None)]


def test_presents_the_optin_blocks_when_included():
    fake = _FakeUseCase(
        result=_a_card(include=frozenset({"dividend", "performance", "metrics"}))
    )
    resp = _client(fake).get(
        "/stocks/ticker/MU?include=dividend&include=performance&include=metrics"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dividend"] == {"yield_percentage": 0.05, "per_share": 0.46}
    # Performance keeps the finance-style aliases the snapshot uses.
    assert body["performance"] == {
        "1w": 1.5, "1m": 8.0, "3m": 40.0, "6m": 90.0, "ytd": 120.0, "1y": 150.0,
    }
    assert body["metrics"] == {"forward_peg": 0.13}


def test_passes_the_raw_include_params_through_to_the_use_case():
    # Comma-separated values arrive as one raw param; splitting/validating is the
    # use case's job (it owns the vocabulary), not the controller's.
    fake = _FakeUseCase(result=_a_card())
    _client(fake).get("/stocks/ticker/MU?include=dividend,metrics")
    assert fake.calls == [("MU", ["dividend,metrics"])]


def test_dividend_requested_but_fundamentals_unavailable_is_null():
    card = _a_card(include=frozenset({"dividend"}))
    fake = _FakeUseCase(
        result=TickerCard(
            quote=card.quote,
            include=card.include,
            valuation=None,
            profile=None,
            fundamentals=None,  # keyless or failed Finnhub
            performance=None,
        )
    )
    resp = _client(fake).get("/stocks/ticker/MU?include=dividend")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] is None
    assert body["market_cap"] is None
    assert body["dividend"] is None  # requested, but nothing to serve


def test_sets_the_cache_header():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_bad_symbol_or_include_is_a_400():
    fake = _FakeUseCase(error=ValueError("Unknown include(s): earnings."))
    assert _client(fake).get("/stocks/ticker/MU?include=earnings").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("MU", "boom"))
    assert _client(fake).get("/stocks/ticker/MU").status_code == 502
