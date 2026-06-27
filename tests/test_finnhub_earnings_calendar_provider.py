"""Unit tests for the Finnhub earnings-calendar adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter picks
the soonest scheduled event, maps it into a NextEarnings, and translates HTTP
failures into domain errors.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import NextEarnings
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.finnhub_earnings_calendar_provider import (
    FinnhubEarningsCalendarProvider,
)


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


def provider_with(http_client) -> FinnhubEarningsCalendarProvider:
    p = FinnhubEarningsCalendarProvider("dummy-key")
    p._http = http_client
    return p


def _cal(*rows) -> dict:
    return {"earningsCalendar": list(rows)}


def test_picks_soonest_event_and_maps_it():
    # Out of order on purpose — the adapter must return the nearest date.
    http = FakeHttpClient(
        json_data=_cal(
            {"date": "2026-10-29", "epsEstimate": 1.55, "revenueEstimate": 95e9,
             "hour": "amc", "quarter": 4, "year": 2026},
            {"date": "2026-07-30", "epsEstimate": 1.42, "revenueEstimate": 89e9,
             "hour": "bmo", "quarter": 3, "year": 2026},
        )
    )
    n = provider_with(http).get_next_earnings("AAPL")
    assert isinstance(n, NextEarnings)
    assert n.report_date == date(2026, 7, 30)
    assert n.eps_estimate == 1.42
    assert n.revenue_estimate == 89e9
    assert n.fiscal_quarter == 3
    assert n.fiscal_year == 2026
    assert n.session == "bmo"


def test_sends_symbol_window_and_token():
    http = FakeHttpClient(json_data=_cal({"date": "2026-07-30", "epsEstimate": 1.42}))
    provider_with(http).get_next_earnings("AAPL")
    url, params = http.requests[0]
    assert url == "/calendar/earnings"
    assert params["symbol"] == "AAPL"
    assert params["token"] == "dummy-key"
    assert "from" in params and "to" in params  # bounded window


def test_empty_calendar_returns_none():
    p = provider_with(FakeHttpClient(json_data=_cal()))
    assert p.get_next_earnings("AAPL") is None


def test_rows_without_a_date_return_none():
    http = FakeHttpClient(json_data=_cal({"epsEstimate": 1.0, "date": None}))
    assert provider_with(http).get_next_earnings("AAPL") is None


def test_unknown_session_normalizes_to_none():
    http = FakeHttpClient(
        json_data=_cal({"date": "2026-07-30", "epsEstimate": 1.0, "hour": ""})
    )
    assert provider_with(http).get_next_earnings("AAPL").session is None


def test_missing_estimate_still_returns_the_date():
    # A scheduled report with no consensus yet is still worth surfacing.
    http = FakeHttpClient(
        json_data=_cal({"date": "2026-07-30", "quarter": 3, "year": 2026})
    )
    n = provider_with(http).get_next_earnings("AAPL")
    assert n.report_date == date(2026, 7, 30)
    assert n.eps_estimate is None


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=429, text="rate limit")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_next_earnings("AAPL")


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_next_earnings("AAPL")


def test_invalid_json_raises_unavailable():
    http = FakeHttpClient(json_error=ValueError("not json"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_next_earnings("AAPL")


def test_recent_revenue_maps_reported_quarters():
    http = FakeHttpClient(
        json_data=_cal(
            {"year": 2026, "quarter": 1, "revenueEstimate": 95e9,
             "revenueActual": 97e9, "date": "2026-04-30"},
            {"year": 2025, "quarter": 4, "revenueEstimate": 88e9,
             "revenueActual": None, "date": "2026-01-28"},
        )
    )
    rev = provider_with(http).get_recent_revenue("AAPL")
    assert rev[(2026, 1)] == (95e9, 97e9)
    assert rev[(2025, 4)] == (88e9, None)


def test_recent_revenue_skips_rows_without_year_or_quarter():
    http = FakeHttpClient(json_data=_cal({"revenueActual": 50e9, "date": "2026-01-28"}))
    assert provider_with(http).get_recent_revenue("AAPL") == {}


def test_recent_revenue_skips_rows_with_no_revenue_figures():
    http = FakeHttpClient(
        json_data=_cal(
            {"year": 2026, "quarter": 1, "revenueEstimate": None, "revenueActual": None}
        )
    )
    assert provider_with(http).get_recent_revenue("AAPL") == {}


def test_recent_revenue_sends_a_past_window():
    http = FakeHttpClient(json_data=_cal())
    provider_with(http).get_recent_revenue("AAPL")
    url, params = http.requests[0]
    assert url == "/calendar/earnings"
    assert params["symbol"] == "AAPL"
    assert "from" in params and "to" in params


def test_recent_revenue_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=429, text="rate limit")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_recent_revenue("AAPL")
