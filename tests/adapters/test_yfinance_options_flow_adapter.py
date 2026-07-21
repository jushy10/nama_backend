from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.stocks.adapters.yfinance.options_flow_adapter import (
    YfinanceOptionsChainProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.options.entities import OptionType

_EXPIRY = date(2026, 7, 31)


class _FakeTicker:
    def __init__(self, options=(), chain=None, error=None) -> None:
        self._options = options
        self._chain = chain
        self._error = error
        self.chain_requests: list[str] = []

    @property
    def options(self):
        if self._error is not None:
            raise self._error
        return self._options

    def option_chain(self, expiration: str):
        self.chain_requests.append(expiration)
        if self._error is not None:
            raise self._error
        return self._chain


def _provider(ticker: _FakeTicker) -> YfinanceOptionsChainProvider:
    return YfinanceOptionsChainProvider(ticker_factory=lambda symbol: ticker)


def _chain(calls, puts, underlying=None):
    # yfinance returns a named tuple with .calls/.puts DataFrames and an .underlying dict.
    return SimpleNamespace(
        calls=pd.DataFrame(calls),
        puts=pd.DataFrame(puts),
        underlying=underlying if underlying is not None else {},
    )


def test_expirations_are_parsed_and_sorted():
    ticker = _FakeTicker(options=("2026-10-02", "2026-07-31", "2027-01-15"))
    assert _provider(ticker).get_expirations("AAPL") == (
        date(2026, 7, 31),
        date(2026, 10, 2),
        date(2027, 1, 15),
    )


def test_no_listed_options_is_empty_coverage_not_an_error():
    assert _provider(_FakeTicker(options=())).get_expirations("ZZZZ") == ()


def test_unparseable_expiration_labels_are_dropped():
    ticker = _FakeTicker(options=("2026-07-31", "soon", ""))
    assert _provider(ticker).get_expirations("AAPL") == (date(2026, 7, 31),)


def test_chain_maps_both_sides_and_reads_spot():
    ticker = _FakeTicker(
        chain=_chain(
            calls=[
                {
                    "strike": 100.0, "bid": 2.8, "ask": 3.2, "lastPrice": 3.1,
                    "volume": 500, "openInterest": 1200, "impliedVolatility": 0.25,
                    "inTheMoney": True,
                }
            ],
            puts=[
                {
                    "strike": 100.0, "bid": 1.9, "ask": 2.1, "lastPrice": 2.0,
                    "volume": 1000, "openInterest": 800, "impliedVolatility": 0.27,
                    "inTheMoney": False,
                }
            ],
            underlying={"regularMarketPrice": 101.5},
        )
    )
    result = _provider(ticker).get_chain("AAPL", _EXPIRY)
    assert ticker.chain_requests == ["2026-07-31"]  # Yahoo takes the ISO label
    assert result.expiration == _EXPIRY
    assert result.spot == 101.5
    assert len(result.contracts) == 2
    (call,) = result.calls
    (put,) = result.puts
    assert call.option_type is OptionType.CALL and put.option_type is OptionType.PUT
    assert (call.strike, call.bid, call.ask, call.last_price) == (100.0, 2.8, 3.2, 3.1)
    assert (call.volume, call.open_interest) == (500, 1200)
    assert call.implied_volatility == 0.25
    assert call.in_the_money is True
    assert call.mid == pytest.approx(3.0)
    assert put.volume == 1000
    assert put.in_the_money is False


def test_spot_falls_back_across_underlying_keys_and_defaults_none():
    # No regularMarketPrice -> take the prior close; nothing usable -> None.
    ticker = _FakeTicker(
        chain=_chain(calls=[{"strike": 100.0}], puts=[], underlying={"regularMarketPreviousClose": 99.0})
    )
    assert _provider(ticker).get_chain("AAPL", _EXPIRY).spot == 99.0
    bare = _FakeTicker(chain=_chain(calls=[{"strike": 100.0}], puts=[], underlying={}))
    assert _provider(bare).get_chain("AAPL", _EXPIRY).spot is None


def test_nan_and_missing_fields_are_absent_not_zero():
    ticker = _FakeTicker(
        chain=_chain(
            calls=[{"strike": 100.0, "bid": float("nan"), "volume": float("nan")}],
            puts=[],
        )
    )
    (call,) = _provider(ticker).get_chain("AAPL", _EXPIRY).contracts
    assert call.bid is None
    assert call.ask is None  # column missing entirely
    assert call.volume is None  # unreported, not zero
    assert call.implied_volatility is None
    assert call.in_the_money is None


def test_rows_without_a_usable_strike_are_dropped():
    ticker = _FakeTicker(
        chain=_chain(
            calls=[{"strike": float("nan"), "bid": 1.0}, {"strike": 100.0, "bid": 1.0}],
            puts=[{"strike": 0.0, "bid": 1.0}],
        )
    )
    contracts = _provider(ticker).get_chain("AAPL", _EXPIRY).contracts
    assert [c.strike for c in contracts] == [100.0]


def test_empty_frames_yield_an_empty_chain():
    ticker = _FakeTicker(chain=_chain(calls=[], puts=[]))
    result = _provider(ticker).get_chain("AAPL", _EXPIRY)
    assert result.contracts == ()


def test_vendor_failure_raises_unavailable():
    boom = _FakeTicker(error=RuntimeError("rate limited"))
    with pytest.raises(StockDataUnavailable):
        _provider(boom).get_expirations("AAPL")
    with pytest.raises(StockDataUnavailable):
        _provider(boom).get_chain("AAPL", _EXPIRY)


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = YfinanceOptionsChainProvider(ticker_factory=_boom)
    with pytest.raises(StockDataUnavailable):
        provider.get_expirations("AAPL")
