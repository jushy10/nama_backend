"""Unit tests for the FMP earnings-estimates adapter (/stable/earnings).

No network: the httpx client is swapped for a fake. Far-future/past dates keep
the upcoming-vs-reported split deterministic regardless of the real today.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import EarningsEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.fmp_estimates_provider import FmpEstimatesProvider


class FakeHttpClient:
    def __init__(
        self, status_code=200, json_data=None, text="", error=None, json_error=None
    ):
        self._status_code = status_code
        self._json = [] if json_data is None else json_data
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


def provider_with(http) -> FmpEstimatesProvider:
    p = FmpEstimatesProvider("dummy-key")
    p._http = http
    return p


def test_splits_upcoming_and_reported_revenue():
    rows = [
        {"date": "2099-09-30", "epsEstimated": 2.1, "revenueEstimated": 120e9,
         "epsActual": None, "revenueActual": None},  # future
        {"date": "2020-03-31", "epsEstimated": 1.0, "revenueEstimated": 90e9,
         "epsActual": 1.05, "revenueActual": 91e9},  # reported
    ]
    est = provider_with(FakeHttpClient(json_data=rows)).get_estimates("AAPL")
    assert isinstance(est, EarningsEstimates)
    assert [u.report_date for u in est.upcoming] == [date(2099, 9, 30)]
    assert est.upcoming[0].eps_estimate == 2.1
    assert est.upcoming[0].revenue_estimate == 120e9
    # reported revenue is tagged by announcement date for proximity matching
    assert est.reported_revenue == ((date(2020, 3, 31), 90e9, 91e9),)


def test_orders_and_caps_upcoming():
    rows = [
        {"date": f"209{i}-03-31", "epsEstimated": 1.0, "revenueEstimated": 1e9}
        for i in range(6)
    ]
    est = provider_with(FakeHttpClient(json_data=rows)).get_estimates("AAPL")
    assert len(est.upcoming) == 4  # capped
    assert est.upcoming[0].report_date < est.upcoming[1].report_date  # nearest first


def test_skips_reported_rows_without_any_revenue():
    rows = [{"date": "2020-03-31", "epsActual": 1.0, "revenueEstimated": None,
             "revenueActual": None}]
    est = provider_with(FakeHttpClient(json_data=rows)).get_estimates("AAPL")
    assert est.reported_revenue == ()


def test_sends_symbol_limit_and_key():
    http = FakeHttpClient(json_data=[])
    provider_with(http).get_estimates("AAPL")
    url, params = http.requests[0]
    assert url == "/stable/earnings"
    assert params["symbol"] == "AAPL"
    assert params["apikey"] == "dummy-key"
    assert params["limit"] == 5  # FMP free-tier cap; higher returns HTTP 402


def test_error_object_degrades_to_empty():
    # FMP returns an error *object* (not a list) for some failures — treat empty.
    est = provider_with(
        FakeHttpClient(json_data={"Error Message": "no data"})
    ).get_estimates("AAPL")
    assert est.upcoming == ()
    assert est.reported_revenue == ()


def test_non_200_raises_unavailable():
    # e.g. a 402 Premium gate — surfaces as a domain error (use case swallows it).
    http = FakeHttpClient(status_code=402, text="Premium endpoint")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_estimates("AAPL")


def test_transport_error_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(FakeHttpClient(error=httpx.ConnectError("boom"))).get_estimates(
            "AAPL"
        )
