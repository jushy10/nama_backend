"""Unit tests for the Treasury par-yield curve adapter.

No network: the httpx client is swapped for a fake returning canned per-URL CSV
text (the ``_http`` seam), and ``_today`` is pinned so the requested year is
deterministic. Verifies the latest row is chosen regardless of row order,
columns map to labelled tenors sorted shortest-first, blank cells and unknown
columns are dropped, an empty current-year file falls back to the prior year,
and transport/non-200 failures raise the domain error.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters import treasury_yield_curve_adapter as tr
from app.stocks.adapters.treasury_yield_curve_adapter import TreasuryYieldCurveProvider
from app.stocks.exceptions import StockDataUnavailable


def _url(year: int) -> str:
    return (tr._BASE_URL + tr._QUERY).format(year=year)


class FakeHttpClient:
    """Fake httpx client: canned CSV text per requested URL, optional status/error overrides."""

    def __init__(self, *, pages=None, status=None, errors=()):
        self._pages = pages or {}
        self._status = status or {}
        self._errors = set(errors)
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if url in self._errors:
            raise httpx.ConnectError("boom")
        return SimpleNamespace(
            status_code=self._status.get(url, 200), text=self._pages.get(url, "")
        )


def _provider(http, *, today=date(2026, 7, 13)) -> TreasuryYieldCurveProvider:
    p = TreasuryYieldCurveProvider()
    p._http = http
    p._today = lambda: today
    return p


def test_reads_latest_row_and_maps_sorted_tenors():
    # Oldest row first: the adapter must still select the max date, not row order.
    csv_text = (
        "Date,1 Mo,4 Mo,2 Yr,10 Yr,Foo\n"
        "07/10/2026,3.70,,4.21,4.56,ignore\n"
        "07/13/2026,3.73,,4.26,4.62,ignore\n"
    )
    http = FakeHttpClient(pages={_url(2026): csv_text})
    curve = _provider(http).get_yield_curve()
    assert curve.as_of == date(2026, 7, 13)
    # 4 Mo is blank -> dropped; Foo isn't a maturity -> ignored; sorted by months.
    assert [(t.label, t.rate) for t in curve.tenors] == [
        ("1M", 3.73),
        ("2Y", 4.26),
        ("10Y", 4.62),
    ]
    # derived reads land on the entity
    assert curve.spread_2s10s == 0.36
    assert curve.is_inverted is False


def test_requests_the_current_year_file():
    csv_text = "Date,2 Yr,10 Yr\n07/13/2026,4.26,4.62\n"
    http = FakeHttpClient(pages={_url(2026): csv_text})
    _provider(http).get_yield_curve()
    assert http.requests == [_url(2026)]


def test_empty_current_year_falls_back_to_prior_year():
    # A header-only current-year file (no business day printed yet) falls back to last year.
    http = FakeHttpClient(
        pages={
            _url(2026): "Date,2 Yr,10 Yr\n",
            _url(2025): "Date,2 Yr,10 Yr\n12/31/2025,4.30,4.58\n",
        }
    )
    curve = _provider(http).get_yield_curve()
    assert curve.as_of == date(2025, 12, 31)
    assert http.requests == [_url(2026), _url(2025)]


def test_inverted_curve_is_flagged():
    csv_text = "Date,2 Yr,10 Yr\n07/13/2026,4.80,4.55\n"
    http = FakeHttpClient(pages={_url(2026): csv_text})
    curve = _provider(http).get_yield_curve()
    assert curve.spread_2s10s == -0.25
    assert curve.is_inverted is True


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status={_url(2026): 503})
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_curve()


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(errors=[_url(2026)])
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_curve()


def test_both_years_empty_raises_unavailable():
    http = FakeHttpClient(
        pages={_url(2026): "Date,2 Yr,10 Yr\n", _url(2025): "Date,2 Yr,10 Yr\n"}
    )
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_curve()
