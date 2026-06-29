"""Unit tests for the Finnhub recommendation-trends adapter.

No network: the httpx client is swapped for a fake. Verifies the adapter maps
the buy/hold/sell rows into entities, orders them newest-first, treats an
uncovered symbol as empty coverage (not an error), and turns HTTP failures into
domain errors.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import AnalystRecommendations
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.finnhub_recommendation_provider import FinnhubRecommendationProvider


class FakeHttpClient:
    def __init__(
        self, status_code=200, json_data=None, text="", error=None, json_error=None
    ):
        self._status_code = status_code
        # The recommendation endpoint returns a bare JSON list, not an object.
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


def provider_with(http_client) -> FinnhubRecommendationProvider:
    p = FinnhubRecommendationProvider("dummy-key")
    p._http = http_client
    return p


def _row(period, *, strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0) -> dict:
    return {
        "period": period,
        "strongBuy": strong_buy,
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "strongSell": strong_sell,
        "symbol": "AAPL",
    }


def test_maps_rows_and_orders_newest_first():
    # Out of order on purpose — the adapter must return newest period first.
    http = FakeHttpClient(
        json_data=[
            _row("2026-05-01", strong_buy=10, buy=15, hold=5, sell=1),
            _row("2026-06-01", strong_buy=13, buy=24, hold=7),
        ]
    )
    recs = provider_with(http).get_recommendations("AAPL")
    assert isinstance(recs, AnalystRecommendations)
    assert [t.period for t in recs.trends] == [date(2026, 6, 1), date(2026, 5, 1)]
    latest = recs.latest
    assert (latest.strong_buy, latest.buy, latest.hold) == (13, 24, 7)
    assert latest.total == 44


def test_sends_symbol_and_token():
    http = FakeHttpClient(json_data=[_row("2026-06-01", buy=1)])
    provider_with(http).get_recommendations("AAPL")
    url, params = http.requests[0]
    assert url == "/stock/recommendation"
    assert params["symbol"] == "AAPL"
    assert params["token"] == "dummy-key"


def test_empty_list_is_no_coverage_not_an_error():
    recs = provider_with(FakeHttpClient(json_data=[])).get_recommendations("ZZZZ")
    assert recs.trends == ()
    assert recs.latest is None
    assert recs.direction is None


def test_rows_without_a_period_are_dropped():
    http = FakeHttpClient(json_data=[_row(None, buy=5), _row("2026-06-01", buy=5)])
    recs = provider_with(http).get_recommendations("AAPL")
    assert [t.period for t in recs.trends] == [date(2026, 6, 1)]


def test_missing_counts_default_to_zero():
    http = FakeHttpClient(json_data=[{"period": "2026-06-01", "buy": 3}])
    t = provider_with(http).get_recommendations("AAPL").latest
    assert t.buy == 3
    assert t.strong_buy == t.hold == t.sell == t.strong_sell == 0


def test_non_list_payload_is_treated_as_empty():
    # An unknown symbol can come back as {} rather than [] — treat it as no data.
    recs = provider_with(FakeHttpClient(json_data={})).get_recommendations("ZZZZ")
    assert recs.trends == ()


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=429, text="rate limit")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_recommendations("AAPL")


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_recommendations("AAPL")


def test_invalid_json_raises_unavailable():
    http = FakeHttpClient(json_error=ValueError("not json"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).get_recommendations("AAPL")
