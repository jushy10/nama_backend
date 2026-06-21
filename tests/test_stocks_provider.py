"""Unit tests for the Alpaca adapter.

No network: the real Alpaca clients are swapped for fakes, and the pure
mapping is tested directly. Verifies an adapter's two jobs — translate
Alpaca models -> Stock entity, and Alpaca failures -> domain exceptions.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from alpaca.common.exceptions import APIError

from app.stocks.alpaca_provider import AlpacaStockDataProvider
from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


def make_snapshot():
    return SimpleNamespace(
        latest_trade=SimpleNamespace(
            price=297.86, timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc)
        ),
        latest_quote=SimpleNamespace(bid_price=283.52, ask_price=313.43),
        daily_bar=SimpleNamespace(
            open=298.44, high=300.56, low=295.635, close=297.86, volume=1278873.0
        ),
        previous_daily_bar=SimpleNamespace(close=296.07),
        minute_bar=None,
    )


class FakeDataClient:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def get_stock_snapshot(self, request):
        if self._error is not None:
            raise self._error
        return self._result


class FakeTradingClient:
    def __init__(self, asset=None, error=None):
        self._asset, self._error = asset, error

    def get_asset(self, symbol):
        if self._error is not None:
            raise self._error
        return self._asset


def provider_with(data_client, trading_client) -> AlpacaStockDataProvider:
    # Construction is offline (clients only store credentials); then swap the
    # real clients for fakes so get_stock() makes no network calls.
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    p._data = data_client
    p._trading = trading_client
    return p


def test_to_entity_maps_every_field():
    stock = AlpacaStockDataProvider._to_entity(
        "AAPL", make_snapshot(), "Apple Inc.", "NASDAQ"
    )
    assert isinstance(stock, Stock)
    assert stock.symbol == "AAPL"
    assert stock.price == 297.86
    assert stock.previous_close == 296.07
    assert stock.volume == 1278873  # float -> int
    assert stock.bid == 283.52 and stock.ask == 313.43
    assert stock.change == 1.79


def test_get_stock_happy_path():
    asset = SimpleNamespace(name="Apple Inc.", exchange=SimpleNamespace(value="NASDAQ"))
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}),
        FakeTradingClient(asset=asset),
    )
    stock = p.get_stock("AAPL")
    assert stock.name == "Apple Inc."
    assert stock.exchange == "NASDAQ"
    assert stock.price == 297.86


def test_missing_snapshot_raises_not_found():
    p = provider_with(FakeDataClient(result={"AAPL": None}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_stock("AAPL")


def test_snapshot_without_trade_raises_not_found():
    snap = make_snapshot()
    snap.latest_trade = None
    p = provider_with(FakeDataClient(result={"AAPL": snap}), FakeTradingClient())
    with pytest.raises(StockNotFound):
        p.get_stock("AAPL")


def test_api_error_translated_to_unavailable():
    p = provider_with(FakeDataClient(error=APIError("boom")), FakeTradingClient())
    with pytest.raises(StockDataUnavailable):
        p.get_stock("AAPL")


def test_asset_metadata_failure_is_non_fatal():
    p = provider_with(
        FakeDataClient(result={"AAPL": make_snapshot()}),
        FakeTradingClient(error=APIError("no asset")),
    )
    stock = p.get_stock("AAPL")
    assert stock.name is None
    assert stock.exchange is None
    assert stock.price == 297.86  # market data still returned


# --------------------------- logo (HTTP) ---------------------------

class FakeHttpClient:
    def __init__(self, status_code=200, content=b"", error=None):
        self._status_code, self._content, self._error = status_code, content, error
        self.requested: list[str] = []

    def get(self, url):
        self.requested.append(url)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(status_code=self._status_code, content=self._content)


def provider_with_http(http_client) -> AlpacaStockDataProvider:
    p = AlpacaStockDataProvider("dummy-key", "dummy-secret")
    p._http = http_client
    return p


def test_get_logo_returns_image_bytes():
    http = FakeHttpClient(status_code=200, content=b"\x89PNG\r\n")
    p = provider_with_http(http)
    assert p.get_logo("AAPL") == b"\x89PNG\r\n"
    assert http.requested == ["/logos/AAPL"]


def test_get_logo_404_raises_not_found():
    p = provider_with_http(FakeHttpClient(status_code=404))
    with pytest.raises(StockNotFound):
        p.get_logo("ZZZZ")


def test_get_logo_other_status_raises_unavailable():
    p = provider_with_http(FakeHttpClient(status_code=500))
    with pytest.raises(StockDataUnavailable):
        p.get_logo("AAPL")


def test_get_logo_transport_error_raises_unavailable():
    p = provider_with_http(FakeHttpClient(error=httpx.ConnectError("boom")))
    with pytest.raises(StockDataUnavailable):
        p.get_logo("AAPL")
