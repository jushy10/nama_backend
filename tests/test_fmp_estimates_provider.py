"""Unit tests for the FMP analyst-estimates adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter keeps the
nearest forward fiscal year (FY1) plus the one after (FY2), drops past years, reads
both the stable and legacy v3 field names, and translates HTTP failures into domain
errors. A fixed ``today`` makes the forward/past split deterministic.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.fmp_estimates_provider import FmpEstimatesProvider

_STABLE = "/stable/analyst-estimates"
_LEGACY = "/api/v3/analyst-estimates/AAPL"


class FakeHttpClient:
    """Maps request paths to canned responses; records calls."""

    def __init__(self, responses=None, default=(200, [], ""), error=None):
        # responses: {path: (status_code, json_payload_or_Exception, text)}
        self._responses = responses or {}
        self._default = default
        self._error = error
        self.requests: list[tuple] = []

    def get(self, url, params=None):
        self.requests.append((url, params or {}))
        if self._error is not None:
            raise self._error
        status, payload, text = self._responses.get(url, self._default)

        def _json():
            if isinstance(payload, Exception):
                raise payload
            return payload

        return SimpleNamespace(status_code=status, text=text, json=_json)


def provider_with(http, today=date(2026, 1, 1)) -> FmpEstimatesProvider:
    p = FmpEstimatesProvider("dummy-key", today=today)
    p._http = http
    return p


def _row(date_str, *, eps=None, eps_low=None, eps_high=None, rev=None, n_eps=None,
         n_rev=None) -> dict:
    """A stable-shape estimate row."""
    return {
        "symbol": "AAPL", "date": date_str,
        "epsAvg": eps, "epsLow": eps_low, "epsHigh": eps_high,
        "revenueAvg": rev, "numAnalystsEps": n_eps, "numAnalystsRevenue": n_rev,
    }


def _stable(*rows) -> FakeHttpClient:
    return FakeHttpClient(responses={_STABLE: (200, list(rows), "")})


def test_picks_nearest_forward_year_as_fy1_then_fy2():
    # Out of order on purpose, with one past year that must be dropped.
    http = _stable(
        _row("2028-09-30", eps=14.0, rev=480e9, n_eps=20, n_rev=18),
        _row("2026-09-30", eps=7.5, eps_low=7.0, eps_high=8.1, rev=420e9,
             n_eps=30, n_rev=28),
        _row("2025-09-30", eps=6.1, rev=400e9),  # past -> excluded
        _row("2027-09-30", eps=9.2, rev=450e9, n_eps=25, n_rev=22),
    )
    est = provider_with(http).get_estimates("AAPL")
    assert isinstance(est, AnalystEstimates)
    # FY1 = the nearest forward year (2026)
    assert est.fiscal_year == 2026
    assert est.period_end == date(2026, 9, 30)
    assert est.eps_avg == 7.5
    assert est.eps_low == 7.0
    assert est.eps_high == 8.1
    assert est.revenue_avg == 420e9
    assert est.num_analysts_eps == 30
    assert est.num_analysts_revenue == 28
    # FY2 = the year after
    assert est.eps_avg_fy2 == 9.2
    assert est.fiscal_year_fy2 == 2027


def test_populates_full_forward_series_for_cagr():
    http = _stable(
        _row("2026-09-30", eps=8.0, rev=420e9),
        _row("2027-09-30", eps=9.2, rev=455e9),
        _row("2028-09-30", eps=11.0, rev=490e9),
        _row("2025-09-30", eps=6.1, rev=400e9),  # past -> excluded from the series
    )
    est = provider_with(http).get_estimates("AAPL")
    assert [(y.fiscal_year, y.eps_avg, y.revenue_avg) for y in est.forward_years] == [
        (2026, 8.0, 420e9), (2027, 9.2, 455e9), (2028, 11.0, 490e9)
    ]
    assert est.forward_eps_cagr() is not None  # derives from the series


def test_forward_pe_and_ps_from_returned_estimates():
    http = _stable(_row("2026-09-30", eps=8.0, rev=400e9))
    est = provider_with(http).get_estimates("AAPL")
    assert est.forward_pe(280.0) == 35.0          # 280 / 8.0
    assert est.forward_ps(2_000e9) == 5.0         # 2.0T / 400B


def test_excludes_all_past_years_as_empty():
    http = _stable(_row("2024-09-30", eps=5.0), _row("2025-09-30", eps=6.0))
    est = provider_with(http).get_estimates("AAPL")
    assert est.is_empty
    assert est.eps_avg is None


def test_empty_list_returns_empty_estimates():
    est = provider_with(_stable()).get_estimates("AAPL")
    assert est.is_empty


def test_sends_symbol_period_and_token():
    http = _stable(_row("2026-09-30", eps=8.0))
    provider_with(http).get_estimates("AAPL")
    url, params = http.requests[0]
    assert url == _STABLE
    assert params["symbol"] == "AAPL"
    assert params["period"] == "annual"
    assert params["apikey"] == "dummy-key"


def test_falls_back_to_legacy_v3_field_names():
    # Stable 403 (legacy-scoped key), then v3 with its different field names.
    http = FakeHttpClient(responses={
        _STABLE: (403, {"Error Message": "legacy"}, "legacy"),
        _LEGACY: (200, [
            {"date": "2026-09-30", "estimatedEpsAvg": 7.5,
             "estimatedRevenueAvg": 420e9, "numberAnalystsEstimatedEps": 30,
             "numberAnalystEstimatedRevenue": 28},
        ], ""),
    })
    est = provider_with(http).get_estimates("AAPL")
    assert est.eps_avg == 7.5
    assert est.revenue_avg == 420e9
    assert est.num_analysts_eps == 30
    assert [u for u, _ in http.requests] == [_STABLE, _LEGACY]  # tried stable first


def test_non_200_on_all_routes_raises_unavailable():
    http = FakeHttpClient(default=(429, "rate limit", "rate limit"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_estimates("AAPL")


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_estimates("AAPL")


def test_invalid_json_on_all_routes_raises_unavailable():
    http = FakeHttpClient(default=(200, ValueError("not json"), ""))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_estimates("AAPL")
