"""Unit tests for the Finnhub earnings-surprise adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter's two
jobs — map a /stock/earnings array into an EarningsHistory, and translate HTTP
failures into domain errors.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import EarningsHistory
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_earnings_provider import FinnhubEarningsProvider


class FakeHttpClient:
    def __init__(
        self, status_code=200, json_data=None, text="", error=None, json_error=None
    ):
        self._status_code = status_code
        self._json = [] if json_data is None else json_data
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


def provider_with(http_client) -> FinnhubEarningsProvider:
    p = FinnhubEarningsProvider("dummy-key")
    p._http = http_client
    return p


_TWO_QUARTERS = [
    {
        "actual": 2.18, "estimate": 2.10, "surprise": 0.08,
        "surprisePercent": 3.81, "period": "2026-03-31", "quarter": 1, "year": 2026,
    },
    {
        "actual": 1.40, "estimate": 1.50, "surprise": -0.10,
        "surprisePercent": -6.67, "period": "2025-12-31", "quarter": 4, "year": 2025,
    },
]


def test_maps_array_into_history_newest_first():
    h = provider_with(FakeHttpClient(json_data=_TWO_QUARTERS)).get_earnings_history(
        "AAPL", limit=4
    )
    assert isinstance(h, EarningsHistory)
    assert h.symbol == "AAPL"
    assert len(h.quarters) == 2
    first = h.quarters[0]
    assert first.period == date(2026, 3, 31)
    assert first.fiscal_year == 2026
    assert first.fiscal_quarter == 1
    assert first.actual == 2.18
    assert first.estimate == 2.10
    assert first.surprise == 0.08
    assert first.surprise_percent == 3.81
    assert first.beat is True            # 2.18 >= 2.10
    assert h.quarters[1].beat is False   # 1.40 < 1.50


def test_sends_symbol_limit_and_token():
    http = FakeHttpClient(json_data=_TWO_QUARTERS)
    provider_with(http).get_earnings_history("AAPL", limit=8)
    url, params = http.requests[0]
    assert url == "/stock/earnings"
    assert params == {"symbol": "AAPL", "limit": 8, "token": "dummy-key"}


def test_missing_period_parses_to_none():
    rows = [{"actual": 1.0, "estimate": 0.9, "period": None, "quarter": 2, "year": 2026}]
    h = provider_with(FakeHttpClient(json_data=rows)).get_earnings_history("AAPL", limit=4)
    assert h.quarters[0].period is None
    assert h.quarters[0].beat is True


def test_empty_array_raises_not_found():
    # An unknown/uncovered symbol comes back as an empty array.
    with pytest.raises(StockNotFound):
        provider_with(FakeHttpClient(json_data=[])).get_earnings_history("ZZZZ", limit=4)


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=429, text="rate limit")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_earnings_history("AAPL", limit=4)


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_earnings_history("AAPL", limit=4)


def test_invalid_json_raises_unavailable():
    http = FakeHttpClient(json_error=ValueError("not json"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_earnings_history("AAPL", limit=4)
