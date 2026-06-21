"""Tests for the stocks vertical slice: entity rules, use case, and the API.

Everything here runs offline. The use case depends on the StockDataProvider
port, so we inject a hand-written FakeProvider instead of mocking Alpaca or
the network — that's the payoff of the clean-architecture layering.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockDataProvider
from app.stocks.router import get_stock_info
from app.stocks.use_cases import GetStockInfo


class FakeProvider(StockDataProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, stock: Stock | None = None, raises: Exception | None = None):
        self._stock = stock
        self._raises = raises
        self.received: list[str] = []

    def get_stock(self, symbol: str) -> Stock:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._stock is not None
        return self._stock


def a_stock(**overrides) -> Stock:
    base = dict(
        symbol="AAPL", name="Apple Inc.", exchange="NASDAQ", price=297.86,
        open=298.44, high=300.56, low=295.635, previous_close=296.07,
        volume=1278873, bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Stock(**base)


# --------------------------- entity rules (pure) ---------------------------

def test_entity_change_and_percent():
    s = a_stock(price=110.0, previous_close=100.0)
    assert s.change == 10.0
    assert s.change_percent == 10.0


def test_entity_change_none_without_previous_close():
    s = a_stock(previous_close=None)
    assert s.change is None
    assert s.change_percent is None


def test_entity_change_percent_guards_zero_division():
    assert a_stock(previous_close=0).change_percent is None


def test_entity_spread():
    assert a_stock(bid=283.52, ask=313.43).spread == 29.91
    assert a_stock(bid=None).spread is None


# --------------------------- use case ---------------------------

def test_use_case_normalizes_symbol():
    fake = FakeProvider(stock=a_stock())
    GetStockInfo(fake).execute("  aapl ")
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_use_case_rejects_invalid_symbols(bad):
    fake = FakeProvider(stock=a_stock())
    with pytest.raises(ValueError):
        GetStockInfo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_use_case_propagates_not_found():
    fake = FakeProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockInfo(fake).execute("ZZZZ")


# --------------------------- API ---------------------------

@pytest.fixture
def make_client():
    def _make(provider: StockDataProvider) -> TestClient:
        app.dependency_overrides[get_stock_info] = lambda: GetStockInfo(provider)
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_get_stock_returns_200_with_computed_fields(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    r = client.get("/stocks/AAPL")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["name"] == "Apple Inc."
    assert body["price"] == 297.86
    assert body["change"] == 1.79          # entity rule, surfaced by the presenter
    assert body["change_percent"] == 0.6
    assert body["spread"] == 29.91


def test_get_stock_normalizes_lowercase(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    assert client.get("/stocks/aapl").json()["symbol"] == "AAPL"


def test_get_stock_invalid_symbol_400(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    assert client.get("/stocks/123").status_code == 400


def test_get_stock_unknown_symbol_404(make_client):
    client = make_client(FakeProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ").status_code == 404


def test_get_stock_upstream_failure_502(make_client):
    client = make_client(FakeProvider(raises=StockDataUnavailable("AAPL", "boom")))
    assert client.get("/stocks/AAPL").status_code == 502
