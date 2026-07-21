from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters.fred_vix_adapter import FredVixProvider, _parse_observations
from app.stocks.exceptions import StockDataUnavailable


class FakeHttpClient:
    def __init__(self, *, text="", status=200, error=None):
        self._text = text
        self._status = status
        self._error = error
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(status_code=self._status, text=self._text)


def _provider(http) -> FredVixProvider:
    p = FredVixProvider()
    p._http = http
    return p


def test_latest_two_observations_become_value_and_previous_close():
    csv = "observation_date,VIXCLS\n2026-07-09,15.84\n2026-07-10,15.03\n2026-07-13,17.16\n"
    snap = _provider(FakeHttpClient(text=csv)).get_vix()
    assert snap.as_of == date(2026, 7, 13)
    assert snap.value == 17.16
    assert snap.previous_close == 15.03
    # derived reads
    assert snap.change == 2.13
    assert snap.regime == "normal"  # 15–20


def test_missing_marker_rows_dropped_and_rows_sorted():
    # a `.` row (FRED's missing marker) plus rows out of order
    csv = "observation_date,VIXCLS\n2026-07-13,17.16\n2026-07-11,.\n2026-07-10,15.03\n"
    obs = _parse_observations(csv)
    assert obs == [(date(2026, 7, 10), 15.03), (date(2026, 7, 13), 17.16)]


def test_single_observation_yields_null_previous_close():
    csv = "observation_date,VIXCLS\n2026-07-13,17.16\n"
    snap = _provider(FakeHttpClient(text=csv)).get_vix()
    assert snap.value == 17.16
    assert snap.previous_close is None
    assert snap.change is None


def test_requests_the_vixcls_series():
    http = FakeHttpClient(text="observation_date,VIXCLS\n2026-07-13,17.16\n")
    _provider(http).get_vix()
    assert http.requests == ["https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"]


def test_empty_file_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(text="observation_date,VIXCLS\n")).get_vix()


def test_non_200_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(status=500, text="")).get_vix()


def test_transport_error_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(error=httpx.ConnectError("boom"))).get_vix()
