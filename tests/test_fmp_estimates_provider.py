"""Unit tests for the FMP analyst-estimates adapter.

No network: the httpx client is swapped for a fake keyed by URL path, so the two
endpoints (analyst-estimates, income-statement) can return different payloads.
Far-future/past dates keep the upcoming-vs-reported split deterministic.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import EarningsEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.fmp_estimates_provider import FmpEstimatesProvider


class FakeHttp:
    """Routes get() by URL substring to a configured (status, json) response."""

    def __init__(self, routes, error=None):
        self._routes = routes  # list[(substr, status, json_data)]
        self._error = error
        self.requests: list[tuple] = []

    def get(self, url, params=None):
        self.requests.append((url, params or {}))
        if self._error is not None:
            raise self._error
        for substr, status, data in self._routes:
            if substr in url:
                return SimpleNamespace(status_code=status, text="err", json=lambda d=data: d)
        return SimpleNamespace(status_code=404, text="not found", json=lambda: {})


def provider_with(http) -> FmpEstimatesProvider:
    p = FmpEstimatesProvider("dummy-key")
    p._http = http
    return p


def test_builds_upcoming_and_reported_revenue():
    http = FakeHttp([
        ("analyst-estimates", 200, [
            {"date": "2099-09-30", "epsAvg": 2.1, "revenueAvg": 120e9},   # future
            {"date": "2099-12-31", "epsAvg": 2.4, "revenueAvg": 125e9},   # future
            {"date": "2020-03-31", "epsAvg": 1.0, "revenueAvg": 90e9},    # past
        ]),
        ("income-statement", 200, [
            {"date": "2020-03-31", "revenue": 91e9},
        ]),
    ])
    est = provider_with(http).get_estimates("AAPL")
    assert isinstance(est, EarningsEstimates)
    # upcoming: the two future quarters, nearest first, with the consensus EPS
    assert [u.report_date for u in est.upcoming] == [date(2099, 9, 30), date(2099, 12, 31)]
    assert est.upcoming[0].eps_estimate == 2.1
    assert est.upcoming[0].revenue_estimate == 120e9
    assert est.upcoming[0].fiscal_quarter is None  # FMP period end, label by date
    # reported revenue: estimate from analyst-estimates, actual from income stmt
    assert est.revenue_by_period[date(2020, 3, 31)] == (90e9, 91e9)


def test_caps_upcoming_to_a_few_quarters():
    rows = [{"date": f"209{i}-03-31", "epsAvg": 1.0, "revenueAvg": 1e9} for i in range(6)]
    http = FakeHttp([("analyst-estimates", 200, rows), ("income-statement", 200, [])])
    est = provider_with(http).get_estimates("AAPL")
    assert len(est.upcoming) == 4  # capped


def test_falls_back_to_legacy_field_names():
    http = FakeHttp([
        ("analyst-estimates", 200, [
            {"date": "2099-06-30", "estimatedEpsAvg": 3.0, "estimatedRevenueAvg": 200e9},
        ]),
        ("income-statement", 200, []),
    ])
    est = provider_with(http).get_estimates("AAPL")
    assert est.upcoming[0].eps_estimate == 3.0
    assert est.upcoming[0].revenue_estimate == 200e9


def test_empty_payloads_yield_empty_estimates():
    http = FakeHttp([("analyst-estimates", 200, []), ("income-statement", 200, [])])
    est = provider_with(http).get_estimates("AAPL")
    assert est.upcoming == ()
    assert est.revenue_by_period == {}


def test_error_object_degrades_gracefully():
    # FMP returns an error *object* (not a list) for some failures — treat as empty.
    http = FakeHttp([
        ("analyst-estimates", 200, {"Error Message": "no data"}),
        ("income-statement", 200, []),
    ])
    est = provider_with(http).get_estimates("AAPL")
    assert est.upcoming == ()


def test_sends_symbol_and_key():
    http = FakeHttp([("analyst-estimates", 200, []), ("income-statement", 200, [])])
    provider_with(http).get_estimates("AAPL")
    _, params = http.requests[0]
    assert params["apikey"] == "dummy-key"


def test_transport_error_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(FakeHttp([], error=httpx.ConnectError("boom"))).get_estimates("AAPL")
