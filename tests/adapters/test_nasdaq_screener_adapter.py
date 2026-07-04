"""Unit tests for the Nasdaq screener adapter (/api/screener/stocks).

No network: the httpx client is swapped for a fake. Verifies rows above the market-cap floor
map to ``ScreenedStock``, the floor filters, blank/garbage market caps and bad symbols are
dropped, the market-cap string is parsed (``$`` + thousands separators), and HTTP / shape
failures become domain errors.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters.nasdaq_screener_adapter import NasdaqScreenerProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock


class FakeHttpClient:
    def __init__(
        self,
        *,
        status_code=200,
        rows=None,
        payload=None,
        text="",
        error=None,
        json_error=None,
    ):
        self._status_code = status_code
        self._payload = payload if payload is not None else {"data": {"rows": rows or []}}
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
            return self._payload

        return SimpleNamespace(
            status_code=self._status_code, text=self._text, json=_json
        )


def provider_with(http) -> NasdaqScreenerProvider:
    return NasdaqScreenerProvider(http_client=http)


def _row(symbol, *, name="", market_cap="", sector=""):
    return {"symbol": symbol, "name": name, "marketCap": market_cap, "sector": sector}


def test_maps_rows_above_the_floor_to_entities():
    http = FakeHttpClient(
        rows=[
            _row(
                "AAPL",
                name="Apple Inc. Common Stock",
                market_cap="3,010,000,000,000",
                sector="Technology",
            )
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert out == (
        ScreenedStock(
            ticker="AAPL",
            name="Apple Inc.",  # verbose "… Common Stock" suffix stripped
            exchange=None,
            market_cap=3.01e12,
            sector="Technology",
        ),
    )


def test_cleans_the_equity_class_suffix_into_the_company_name():
    http = FakeHttpClient(
        rows=[
            _row("AAPL", name="Apple Inc. Common Stock ", market_cap="3e12"),  # trailing space
            _row("GOOGL", name="Alphabet Inc. Class A Common Stock", market_cap="2e12"),
            _row("AMTK", name="AMETEK Inc.", market_cap="4e10"),  # no suffix -> unchanged
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert {(s.ticker, s.name) for s in out} == {
        ("AAPL", "Apple Inc."),
        ("GOOGL", "Alphabet Inc."),  # "Class A Common Stock" beats "Common Stock"
        ("AMTK", "AMETEK Inc."),
    }


def test_excludes_preferred_and_warrant_listings():
    http = FakeHttpClient(
        rows=[
            _row("AGNC", name="AGNC Investment Corp. Common Stock", market_cap="8e9"),
            _row(
                "AGNCP",
                name="AGNC Investment Corp. Depositary Shares Rep Series G Preferred Stock",
                market_cap="8e9",  # Nasdaq stamps the issuer's cap on the preferred line
            ),
            _row("FOOW", name="Foo Corp. Warrant", market_cap="9e9"),
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["AGNC"]  # preferred + warrant tranches dropped


def test_filters_out_names_below_the_floor():
    http = FakeHttpClient(
        rows=[
            _row("BIG", market_cap="6000000000"),
            _row("SMALL", market_cap="4000000000"),
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["BIG"]


def test_drops_rows_with_blank_or_unparseable_market_cap():
    http = FakeHttpClient(
        rows=[
            _row("A", market_cap=""),
            _row("B", market_cap="NA"),
            _row("C", market_cap="$7,000,000,000"),  # parses to 7e9, above the floor
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["C"]
    assert out[0].market_cap == 7_000_000_000.0


def test_skips_bad_symbols_and_upcases_the_ticker():
    http = FakeHttpClient(
        rows=[
            _row("", market_cap="9000000000"),  # blank symbol
            {"marketCap": "9000000000"},  # missing symbol
            _row("HAS SPACE", market_cap="9000000000"),  # space in symbol
            _row("aapl", market_cap="9000000000"),  # lower-cased -> upper
        ]
    )
    out = provider_with(http).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["AAPL"]


def test_requests_the_screener_path_in_download_mode():
    http = FakeHttpClient(rows=[])
    assert provider_with(http).screen(min_market_cap=5_000_000_000) == ()
    url, params = http.requests[0]
    assert url == "/api/screener/stocks"
    assert params.get("download") == "true"


def test_non_200_raises_unavailable():
    http = FakeHttpClient(status_code=403, text="blocked")
    with pytest.raises(StockDataUnavailable):
        provider_with(http).screen(min_market_cap=5_000_000_000)


def test_transport_error_raises_unavailable():
    http = FakeHttpClient(error=httpx.ConnectError("boom"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).screen(min_market_cap=5_000_000_000)


def test_invalid_json_raises_unavailable():
    http = FakeHttpClient(json_error=ValueError("nope"))
    with pytest.raises(StockDataUnavailable):
        provider_with(http).screen(min_market_cap=5_000_000_000)


def test_missing_data_rows_raises_unavailable():
    http = FakeHttpClient(payload={"data": {}})
    with pytest.raises(StockDataUnavailable):
        provider_with(http).screen(min_market_cap=5_000_000_000)
