from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters.fred import yield_history_adapter_impl as fred
from app.stocks.adapters.fred.yield_history_adapter_impl import YieldHistoryAdapterImpl
from app.stocks.exceptions import StockDataUnavailable


def _url(series_id: str) -> str:
    return fred._BASE_URL.format(series_id=series_id)


class FakeHttpClient:
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


def _provider(http, *, today=date(2026, 7, 13)) -> YieldHistoryAdapterImpl:
    p = YieldHistoryAdapterImpl()
    p._http = http
    p._today = lambda: today
    return p


def test_parses_both_series_and_pairs_the_spread():
    http = FakeHttpClient(
        pages={
            _url("DGS2"): "observation_date,DGS2\n2026-07-01,4.20\n2026-07-02,4.26\n",
            _url("DGS10"): "observation_date,DGS10\n2026-07-01,4.55\n2026-07-02,4.62\n",
        }
    )
    history = _provider(http).get_yield_history(3650)
    assert [s.label for s in history.series] == ["2Y", "10Y"]
    assert history.series[0].observations[0].on == date(2026, 7, 1)
    assert history.series[0].observations[0].rate == 4.20
    # spread derived on shared dates
    assert history.latest_spread == 0.36
    assert history.is_inverted is False


def test_drops_missing_marker_rows_and_sorts_chronologically():
    http = FakeHttpClient(
        pages={
            # a `.` row (FRED's missing marker) and out-of-order rows
            _url("DGS2"): "observation_date,DGS2\n2026-07-02,4.26\n2026-07-01,.\n2026-06-30,4.10\n",
            _url("DGS10"): "observation_date,DGS10\n2026-07-02,4.62\n2026-06-30,4.50\n",
        }
    )
    obs = _provider(http).get_yield_history(3650).series[0].observations
    assert [(o.on, o.rate) for o in obs] == [
        (date(2026, 6, 30), 4.10),
        (date(2026, 7, 2), 4.26),
    ]


def test_observations_before_the_cutoff_are_dropped():
    # lookback of 5 days from 2026-07-13 -> cutoff 2026-07-08; older rows drop.
    http = FakeHttpClient(
        pages={
            _url("DGS2"): "observation_date,DGS2\n2026-06-01,4.00\n2026-07-10,4.26\n",
            _url("DGS10"): "observation_date,DGS10\n2026-06-01,4.30\n2026-07-10,4.62\n",
        }
    )
    obs = _provider(http).get_yield_history(5).series[0].observations
    assert [o.on for o in obs] == [date(2026, 7, 10)]


def test_requests_both_series():
    http = FakeHttpClient(
        pages={
            _url("DGS2"): "observation_date,DGS2\n2026-07-10,4.26\n",
            _url("DGS10"): "observation_date,DGS10\n2026-07-10,4.62\n",
        }
    )
    _provider(http).get_yield_history(3650)
    assert http.requests == [_url("DGS2"), _url("DGS10")]


def test_empty_series_raises_unavailable():
    http = FakeHttpClient(
        pages={
            _url("DGS2"): "observation_date,DGS2\n",  # header only
            _url("DGS10"): "observation_date,DGS10\n2026-07-10,4.62\n",
        }
    )
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_history(3650)


def test_non_200_raises_unavailable():
    http = FakeHttpClient(
        pages={_url("DGS10"): "observation_date,DGS10\n2026-07-10,4.62\n"},
        status={_url("DGS2"): 500},
    )
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_history(3650)


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(errors=[_url("DGS2")])
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_yield_history(3650)
