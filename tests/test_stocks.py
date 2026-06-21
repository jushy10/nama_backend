"""Tests for the stocks vertical slice: entity rules, use case, and the API.

Everything here runs offline. The use case depends on the StockDataProvider
port, so we inject a hand-written FakeProvider instead of mocking Alpaca or
the network — that's the payoff of the clean-architecture layering.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stocks.entities import Logo, Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import LogoProvider, StockDataProvider
from app.stocks.router import get_stock_info, get_stock_logo
from app.stocks.use_cases import GetStockInfo, GetStockLogo


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


class FakeLogoProvider(LogoProvider):
    """Returns/raises whatever the test configured; records calls."""

    def __init__(self, logo: Logo | None = None, raises: Exception | None = None):
        self._logo = logo
        self._raises = raises
        self.received: list[str] = []

    def get_logo(self, symbol: str) -> Logo:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._logo is not None
        return self._logo


def a_logo(content: bytes = b"\x89PNG\r\n", media_type: str = "image/png") -> Logo:
    return Logo(content=content, media_type=media_type)


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


def test_logo_use_case_normalizes_symbol():
    fake = FakeLogoProvider(logo=a_logo(content=b"PNG"))
    assert GetStockLogo(fake).execute("  aapl ").content == b"PNG"
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_logo_use_case_rejects_invalid_symbols(bad):
    fake = FakeLogoProvider(logo=a_logo())
    with pytest.raises(ValueError):
        GetStockLogo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


# --------------------------- API ---------------------------

@pytest.fixture
def make_client():
    def _make(
        provider: StockDataProvider | None = None,
        logo_provider: LogoProvider | None = None,
    ) -> TestClient:
        if provider is not None:
            app.dependency_overrides[get_stock_info] = lambda: GetStockInfo(provider)
        if logo_provider is not None:
            app.dependency_overrides[get_stock_logo] = lambda: GetStockLogo(logo_provider)
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


# --------------------------- logo endpoint ---------------------------

def test_get_logo_returns_png_bytes(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo(content=b"\x89PNG\r\n")))
    r = client.get("/stocks/AAPL/logo")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG\r\n"


def test_get_logo_passes_through_media_type(make_client):
    svg = a_logo(content=b"<svg/>", media_type="image/svg+xml")
    client = make_client(logo_provider=FakeLogoProvider(svg))
    r = client.get("/stocks/AAPL/logo")
    assert r.headers["content-type"] == "image/svg+xml"


def test_get_logo_invalid_symbol_400(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo()))
    assert client.get("/stocks/123/logo").status_code == 400


def test_get_logo_missing_404(make_client):
    client = make_client(logo_provider=FakeLogoProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/logo").status_code == 404


def test_get_logo_upstream_failure_502(make_client):
    fake = FakeLogoProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(logo_provider=fake)
    assert client.get("/stocks/AAPL/logo").status_code == 502


# --------------------------- CORS ---------------------------

def test_cors_allows_configured_origin(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    origin = "https://namainsights.com"
    r = client.get("/stocks/AAPL", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin


def test_cors_preflight_succeeds(make_client):
    client = make_client(FakeProvider(stock=a_stock()))
    r = client.options(
        "/stocks/AAPL",
        headers={
            "Origin": "https://namainsights.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200  # was 405 before CORSMiddleware
    assert r.headers["access-control-allow-origin"] == "https://namainsights.com"
