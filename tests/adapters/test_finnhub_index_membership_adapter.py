"""Unit tests for the Finnhub index-membership adapter (/index/constituents).

No network: the httpx client is swapped for a fake returning canned per-index responses (the
same seam the other Finnhub adapter tests use). Verifies both index sets are read + normalized
(``BRK.B`` -> ``BRK-B``), the symbol + token are sent, a single failing index degrades to empty
(the other still syncs), and both failing raises the domain error.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters import finnhub_index_membership_adapter as finnhub
from app.stocks.adapters.finnhub_index_membership_adapter import (
    FinnhubIndexMembershipProvider,
)
from app.stocks.exceptions import StockDataUnavailable


class FakeHttpClient:
    """Fake httpx client: returns a canned response per requested index symbol. Per symbol you
    can supply a constituents list (200), a status_code override, a transport error, or a JSON
    error. Records the (url, params) of every call."""

    def __init__(
        self, *, constituents=None, status=None, errors=(), json_errors=(), text=""
    ):
        self._constituents = constituents or {}  # symbol -> list of tickers
        self._status = status or {}  # symbol -> status_code
        self._errors = set(errors)  # symbols raising a transport error
        self._json_errors = set(json_errors)  # symbols whose .json() raises
        self._text = text
        self.requests: list[tuple] = []

    def get(self, url, params=None):
        params = params or {}
        symbol = params.get("symbol")
        self.requests.append((url, params))
        if symbol in self._errors:
            raise httpx.ConnectError("boom")

        def _json():
            if symbol in self._json_errors:
                raise ValueError("bad json")
            return {"symbol": symbol, "constituents": self._constituents.get(symbol, [])}

        return SimpleNamespace(
            status_code=self._status.get(symbol, 200), text=self._text, json=_json
        )


def _provider(http) -> FinnhubIndexMembershipProvider:
    p = FinnhubIndexMembershipProvider("dummy-key")
    p._http = http
    return p


def test_reads_both_indices_and_normalizes_tickers():
    http = FakeHttpClient(
        constituents={
            finnhub._SP500_SYMBOL: ["AAPL", "MSFT", "BRK.B"],
            finnhub._NASDAQ100_SYMBOL: ["AAPL", "NVDA"],
        }
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL", "MSFT", "BRK-B"})  # dot -> dash
    assert snap.nasdaq100 == frozenset({"AAPL", "NVDA"})


def test_sends_the_index_symbol_and_token():
    http = FakeHttpClient(
        constituents={finnhub._SP500_SYMBOL: ["AAPL"], finnhub._NASDAQ100_SYMBOL: ["AAPL"]}
    )
    _provider(http).fetch()
    urls = {url for url, _ in http.requests}
    symbols = {p["symbol"] for _, p in http.requests}
    tokens = {p["token"] for _, p in http.requests}
    assert urls == {"/index/constituents"}
    assert symbols == {finnhub._SP500_SYMBOL, finnhub._NASDAQ100_SYMBOL}
    assert tokens == {"dummy-key"}


def test_a_transport_error_for_one_index_degrades_to_empty():
    http = FakeHttpClient(
        constituents={finnhub._SP500_SYMBOL: ["AAPL", "MSFT"]},
        errors=[finnhub._NASDAQ100_SYMBOL],
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL", "MSFT"})
    assert snap.nasdaq100 == frozenset()


def test_a_non_200_for_one_index_degrades_to_empty():
    # e.g. a plan that doesn't cover the index -> 403 for that symbol; the other still syncs.
    http = FakeHttpClient(
        constituents={finnhub._SP500_SYMBOL: ["AAPL"]},
        status={finnhub._NASDAQ100_SYMBOL: 403},
        text="access denied",
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL"})
    assert snap.nasdaq100 == frozenset()


def test_bad_json_for_one_index_degrades_to_empty():
    http = FakeHttpClient(
        constituents={finnhub._SP500_SYMBOL: ["AAPL"]},
        json_errors=[finnhub._NASDAQ100_SYMBOL],
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL"})
    assert snap.nasdaq100 == frozenset()


def test_both_indices_failing_raises_unavailable():
    http = FakeHttpClient(errors=[finnhub._SP500_SYMBOL, finnhub._NASDAQ100_SYMBOL])
    with pytest.raises(StockDataUnavailable):
        _provider(http).fetch()


def test_an_empty_constituents_list_is_an_empty_set():
    http = FakeHttpClient(
        constituents={finnhub._SP500_SYMBOL: ["AAPL"], finnhub._NASDAQ100_SYMBOL: []}
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL"})
    assert snap.nasdaq100 == frozenset()
