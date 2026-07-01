"""Unit tests for the Finnhub company-profile adapter (/stock/profile2).

No network: the httpx client is swapped for a fake. Verifies the clean name is
extracted, an unknown symbol degrades to no name, and HTTP failures become
domain errors.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.finnhub_company_profile_provider import FinnhubCompanyProfileProvider


class FakeHttpClient:
    def __init__(
        self, status_code=200, json_data=None, text="", error=None, json_error=None
    ):
        self._status_code = status_code
        self._json = {} if json_data is None else json_data
        self._text = text
        self._error = error
        self._json_error = json_error
        self.requests: list[tuple] = []

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


def provider_with(http) -> FinnhubCompanyProfileProvider:
    p = FinnhubCompanyProfileProvider("dummy-key")
    p._http = http
    return p


def test_returns_clean_name():
    http = FakeHttpClient(
        json_data={"name": "Apple Inc.", "ticker": "AAPL", "finnhubIndustry": "Tech"}
    )
    profile = provider_with(http).get_profile("AAPL")
    assert isinstance(profile, CompanyProfile)
    assert profile.name == "Apple Inc."


def test_unknown_symbol_yields_no_name():
    # Finnhub returns an empty object for an unknown symbol.
    assert provider_with(FakeHttpClient(json_data={})).get_profile("ZZZZ").name is None


def test_blank_name_normalizes_to_none():
    http = FakeHttpClient(json_data={"name": "   "})
    assert provider_with(http).get_profile("AAPL").name is None


def test_sends_symbol_and_token():
    http = FakeHttpClient(json_data={"name": "Apple Inc."})
    provider_with(http).get_profile("AAPL")
    url, params = http.requests[0]
    assert url == "/stock/profile2"
    assert params["symbol"] == "AAPL"
    assert params["token"] == "dummy-key"


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=429, text="rate limit")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_profile("AAPL")


def test_transport_error_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(FakeHttpClient(error=httpx.ConnectError("boom"))).get_profile(
            "AAPL"
        )


def test_invalid_json_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(FakeHttpClient(json_error=ValueError("nope"))).get_profile("AAPL")
