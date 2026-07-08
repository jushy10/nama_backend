"""Tests for the yfinance ETF profile adapter (fund profile from Ticker.info + funds_data).

Offline: a fake Ticker (with a fake ``funds_data``) is injected through the adapter's
``ticker_factory`` seam, so this exercises the field mapping, the per-field unit normalization
(Yahoo mixes fractions and already-percent numbers — verified empirically against VOO), the
holdings/sector shaping, and the failure contract — **raises on a hard ``.info`` read** (the sync's
signal to skip and retry the fund), best-effort past that — without touching Yahoo.
"""

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_etf_profile_adapter import (
    YfinanceEtfProfileProvider,
)
from app.stocks.exceptions import StockDataUnavailable

# The VOO ``.info`` shape, at the raw units Yahoo actually returns (see the adapter's docstring):
# netExpenseRatio/ytdReturn are already-percent numbers, the yield/return averages are fractions.
_VOO_INFO = {
    "category": "Large Blend",  # display label -> slugged to "large_blend"
    "fundFamily": "Vanguard",
    "totalAssets": 1_701_513_003_008,
    "netExpenseRatio": 0.03,  # already a percent -> as-is
    "navPrice": 685.28,
    "yield": 0.0103,  # a fraction -> x100 -> 1.03
    "ytdReturn": 11.25,  # already a percent -> as-is (NOT x100)
    "threeYearAverageReturn": 0.204,  # a fraction -> x100 -> 20.4
    "fiveYearAverageReturn": 0.130,  # a fraction -> x100 -> 13.0
}


def _holdings_frame(rows):
    """Build a ``top_holdings``-shaped DataFrame: indexed by holding symbol, with Name + Holding
    Percent columns (the shape yfinance's funds_data returns)."""
    frame = pd.DataFrame(
        [{"Name": name, "Holding Percent": pct} for _, name, pct in rows],
        index=[sym for sym, _, _ in rows],
    )
    frame.index.name = "Symbol"
    return frame


class _FakeFundsData:
    """A stand-in for ``Ticker.funds_data``: canned description / top_holdings / sector_weightings
    (or raising on attribute access to model a fund with no fund data)."""

    def __init__(self, *, description=None, top_holdings=None, sector_weightings=None, error=None):
        self._description = description
        self._top_holdings = top_holdings
        self._sector_weightings = sector_weightings
        self._error = error

    def _guard(self):
        if self._error is not None:
            raise self._error

    @property
    def description(self):
        self._guard()
        return self._description

    @property
    def top_holdings(self):
        self._guard()
        return self._top_holdings

    @property
    def sector_weightings(self):
        self._guard()
        return self._sector_weightings


class _FakeTicker:
    """A stand-in for ``yf.Ticker`` exposing a canned ``.info`` + ``.funds_data`` (either may
    raise on access)."""

    def __init__(self, info, funds_data=None, *, info_error=None, funds_error=None):
        self._info = info
        self._funds_data = funds_data if funds_data is not None else _FakeFundsData()
        self._info_error = info_error
        self._funds_error = funds_error

    @property
    def info(self):
        if self._info_error is not None:
            raise self._info_error
        return self._info

    @property
    def funds_data(self):
        if self._funds_error is not None:
            raise self._funds_error
        return self._funds_data


def _provider(ticker: _FakeTicker) -> YfinanceEtfProfileProvider:
    return YfinanceEtfProfileProvider(ticker_factory=lambda symbol: ticker)


def _voo_ticker() -> _FakeTicker:
    funds = _FakeFundsData(
        description="The fund employs an indexing investment approach.",
        top_holdings=_holdings_frame(
            [
                ("NVDA", "NVIDIA Corp", 0.078851),
                ("AAPL", "Apple Inc", 0.0704081),
            ]
        ),
        sector_weightings={
            "technology": 0.3913,
            "financial_services": 0.1092,
            "realestate": 0.0181,
        },
    )
    return _FakeTicker(_VOO_INFO, funds)


def test_maps_and_normalizes_every_info_field_to_human_percent():
    profile = _provider(_voo_ticker()).get_profile("VOO")

    assert profile.category == "large_blend"  # display label slugged
    assert profile.fund_family == "Vanguard"
    assert profile.net_assets == 1_701_513_003_008.0  # raw AUM, passed through
    assert profile.expense_ratio == 0.03  # already a percent -> unchanged
    assert profile.nav == 685.28  # raw price, passed through
    # The x100-normalized figures are unrounded — approx absorbs the float noise.
    assert profile.dividend_yield == pytest.approx(1.03)  # 0.0103 fraction -> x100
    assert profile.ytd_return == 11.25  # already a percent -> NOT x100 (passed through)
    assert profile.three_year_return == pytest.approx(20.4)  # 0.204 fraction -> x100
    assert profile.five_year_return == pytest.approx(13.0)  # 0.130 fraction -> x100
    assert profile.description == "The fund employs an indexing investment approach."


def test_maps_holdings_with_weight_as_percent_preserving_order():
    profile = _provider(_voo_ticker()).get_profile("VOO")
    # The vendor's largest-first order is preserved; ticker/name pass through, weight is the
    # vendor's fraction x100 (unrounded, so compared with approx for the float noise).
    assert [(h.ticker, h.name) for h in profile.top_holdings] == [
        ("NVDA", "NVIDIA Corp"),
        ("AAPL", "Apple Inc"),
    ]
    assert [h.weight for h in profile.top_holdings] == pytest.approx([7.8851, 7.04081])


def test_sector_weightings_are_percent_and_sorted_desc():
    profile = _provider(_voo_ticker()).get_profile("VOO")
    # Normalized to percent (x100, unrounded) and sorted by weight descending regardless of the
    # source dict's order.
    assert [s.sector for s in profile.sector_weightings] == [
        "technology",
        "financial_services",
        "realestate",
    ]
    assert [s.weight for s in profile.sector_weightings] == pytest.approx(
        [39.13, 10.92, 1.81]
    )


def test_caps_holdings_at_ten():
    rows = [(f"H{i}", f"Holding {i}", 0.01 * (30 - i)) for i in range(15)]
    ticker = _FakeTicker(_VOO_INFO, _FakeFundsData(top_holdings=_holdings_frame(rows)))
    profile = _provider(ticker).get_profile("VOO")
    assert len(profile.top_holdings) == 10  # capped, largest-first as the vendor ordered them
    assert profile.top_holdings[0].ticker == "H0"


def test_missing_info_fields_are_null_not_an_error():
    # A sparse .info (fund Yahoo barely covers) leaves each absent field null, still a profile —
    # a served-but-sparse .info does NOT raise (only an empty/failed one does).
    ticker = _FakeTicker({"fundFamily": "iShares"}, _FakeFundsData())
    profile = _provider(ticker).get_profile("IVV")
    assert profile.fund_family == "iShares"
    assert profile.category is None
    assert profile.dividend_yield is None
    assert profile.ytd_return is None
    assert profile.nav is None
    assert profile.top_holdings == ()
    assert profile.sector_weightings == ()


def test_empty_info_raises():
    # yfinance surfaces a swallowed crumb 401 as an empty .info; after the retry it's still empty.
    # The sync must tell this block apart from a served-but-sparse fund, so it's a hard failure.
    ticker = _FakeTicker({}, _FakeFundsData())
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_profile("VOO")


def test_info_hard_failure_raises():
    # A raised vendor error on .info is a hard failure — it raises so the sync skips (and retries)
    # the fund and leaves its stored profile intact.
    ticker = _FakeTicker(None, info_error=RuntimeError("429 Too Many Requests"))
    with pytest.raises(StockDataUnavailable):
        _provider(ticker).get_profile("VOO")


def test_funds_data_failure_yields_a_partial_profile_not_an_error():
    # funds_data can raise for a fund Yahoo carries no fund data for; that's best-effort — the
    # served .info half still yields a profile, with an empty description/holdings/sectors.
    ticker = _FakeTicker(_VOO_INFO, funds_error=RuntimeError("no fund data"))
    profile = _provider(ticker).get_profile("VOO")
    assert profile.fund_family == "Vanguard"  # info half serves
    assert profile.category == "large_blend"
    assert profile.description is None
    assert profile.top_holdings == ()
    assert profile.sector_weightings == ()


def test_none_holdings_and_non_dict_sectors_yield_empty_lists():
    # Defensive shaping: a None holdings frame and a non-dict sector value contribute nothing
    # rather than crashing.
    ticker = _FakeTicker(
        _VOO_INFO,
        _FakeFundsData(top_holdings=None, sector_weightings=None),
    )
    profile = _provider(ticker).get_profile("VOO")
    assert profile.top_holdings == ()
    assert profile.sector_weightings == ()
    # The info half still serves.
    assert profile.fund_family == "Vanguard"
