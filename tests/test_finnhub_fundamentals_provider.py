"""Unit tests for the Finnhub fundamentals adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter's two
jobs — map a /stock/metric payload to StockFundamentals, and translate HTTP
failures into domain errors.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import StockFundamentals
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.finnhub_fundamentals_provider import FinnhubFundamentalsProvider


class FakeHttpClient:
    def __init__(
        self, status_code=200, json_data=None, text="", error=None, json_error=None
    ):
        self._status_code = status_code
        self._json = {} if json_data is None else json_data
        self._text = text
        self._error = error
        self._json_error = json_error
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.requests.append((url, params or {}))
        if self._error is not None:
            raise self._error

        def _json():
            if self._json_error is not None:
                raise self._json_error
            return self._json

        return SimpleNamespace(
            status_code=self._status_code, text=self._text, json=_json
        )


def provider_with(http_client) -> FinnhubFundamentalsProvider:
    p = FinnhubFundamentalsProvider("dummy-key")
    p._http = http_client
    return p


def test_maps_metric_fields():
    http = FakeHttpClient(
        json_data={
            "metric": {
                "marketCapitalization": 3_000_000,  # millions
                "dividendPerShareAnnual": 1.0,
                "dividendYieldIndicatedAnnual": 0.42,
            }
        }
    )
    f = provider_with(http).get_fundamentals("AAPL")
    assert isinstance(f, StockFundamentals)
    assert f.market_cap == 3_000_000 * 1_000_000  # millions -> raw USD
    assert f.dividend_per_share == 1.0
    assert f.dividend_yield == 0.42


def test_sends_symbol_metric_and_token():
    http = FakeHttpClient(json_data={"metric": {}})
    provider_with(http).get_fundamentals("AAPL")
    url, params = http.requests[0]
    assert url == "/stock/metric"
    assert params == {"symbol": "AAPL", "metric": "all", "token": "dummy-key"}


def test_dividend_fields_fall_back_to_ttm():
    http = FakeHttpClient(
        json_data={
            "metric": {"dividendPerShareTTM": 2.5, "currentDividendYieldTTM": 1.1}
        }
    )
    f = provider_with(http).get_fundamentals("AAPL")
    assert f.dividend_per_share == 2.5
    assert f.dividend_yield == 1.1


def test_empty_metric_yields_all_none():
    f = provider_with(FakeHttpClient(json_data={"metric": {}})).get_fundamentals("ZZZZ")
    assert f == StockFundamentals(None, None, None)


def test_missing_metric_key_yields_all_none():
    f = provider_with(FakeHttpClient(json_data={})).get_fundamentals("ZZZZ")
    assert f == StockFundamentals(None, None, None)


def test_non_200_raises_unavailable_with_body():
    http = FakeHttpClient(status_code=429, text="rate limited")
    with pytest.raises(StockDataUnavailable) as exc:
        provider_with(http).get_fundamentals("AAPL")
    assert "429" in str(exc.value)
    assert "rate limited" in str(exc.value)  # upstream body surfaced for debugging


def test_invalid_json_raises_unavailable():
    http = FakeHttpClient(json_error=ValueError("not json"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_fundamentals("AAPL")


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_fundamentals("AAPL")
