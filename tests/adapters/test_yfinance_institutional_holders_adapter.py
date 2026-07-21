from datetime import date

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_institutional_holders_adapter import (
    YfinanceInstitutionalHoldersProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalOwnership,
)

_HOLDER_COLUMNS = ["Date Reported", "Holder", "pctHeld", "Shares", "Value", "pctChange"]


def _holders_frame(rows):
    return pd.DataFrame(rows, columns=_HOLDER_COLUMNS)


def _row(holder, *, reported="2026-06-30", pct_held=0.089, shares=1000, value=100000, pct_change=0.0):
    return [pd.Timestamp(reported), holder, pct_held, shares, value, pct_change]


def _major_frame():
    return pd.DataFrame(
        {
            "Value": {
                "insidersPercentHeld": 0.0007,
                "institutionsPercentHeld": 0.6234,
                "institutionsFloatPercentHeld": 0.6300,
                "institutionsCount": 5321,
            }
        }
    )


class _FakeTicker:
    def __init__(self, *, institutional=None, mutualfund=None, major=None, errors=None) -> None:
        self._institutional = institutional
        self._mutualfund = mutualfund
        self._major = major
        self._errors = errors or {}

    @property
    def institutional_holders(self):
        if "institutional" in self._errors:
            raise self._errors["institutional"]
        return self._institutional

    @property
    def mutualfund_holders(self):
        if "mutualfund" in self._errors:
            raise self._errors["mutualfund"]
        return self._mutualfund

    @property
    def major_holders(self):
        if "major" in self._errors:
            raise self._errors["major"]
        return self._major


def _provider(**kw) -> YfinanceInstitutionalHoldersProvider:
    return YfinanceInstitutionalHoldersProvider(
        ticker_factory=lambda symbol: _FakeTicker(**kw)
    )


def test_maps_fields_converts_fractions_and_orders_newest_largest_first():
    frame = _holders_frame(
        [
            _row("Old Fund", reported="2026-03-31", value=50000),
            _row("Small Q2", reported="2026-06-30", value=20000),
            _row("Vanguard Group Inc", reported="2026-06-30", pct_held=0.089, shares=1234, value=900000, pct_change=0.10),
        ]
    )
    ownership = _provider(institutional=frame).get_institutional_ownership("AAPL")

    assert isinstance(ownership, InstitutionalOwnership)
    # Newest quarter first; within Q2 largest value first; Q1 last.
    assert [h.holder for h in ownership.holders] == ["Vanguard Group Inc", "Small Q2", "Old Fund"]
    top = ownership.holders[0]
    assert top.holder_type == HOLDER_TYPE_INSTITUTION
    assert top.date_reported == date(2026, 6, 30)
    assert top.pct_held == pytest.approx(8.9)  # 0.089 fraction -> percent
    assert top.pct_change == pytest.approx(10.0)  # 0.10 fraction -> percent
    assert (top.shares, top.value) == (1234.0, 900000.0)


def test_combines_institution_and_mutual_fund_feeds():
    ownership = _provider(
        institutional=_holders_frame([_row("BlackRock")]),
        mutualfund=_holders_frame([_row("Vanguard 500 Index Fund")]),
    ).get_institutional_ownership("AAPL")

    by_type = {h.holder: h.holder_type for h in ownership.holders}
    assert by_type == {
        "BlackRock": HOLDER_TYPE_INSTITUTION,
        "Vanguard 500 Index Fund": HOLDER_TYPE_MUTUAL_FUND,
    }


def test_parses_the_breakdown_from_major_holders():
    ownership = _provider(
        institutional=_holders_frame([_row("BlackRock")]),
        major=_major_frame(),
    ).get_institutional_ownership("AAPL")

    b = ownership.breakdown
    assert b is not None
    assert b.institutions_pct_held == pytest.approx(62.34)  # 0.6234 -> percent
    assert b.insiders_pct_held == pytest.approx(0.07)
    assert b.institutions_float_pct_held == pytest.approx(63.0)
    assert b.institutions_count == 5321  # coerced to int


def test_rows_without_holder_or_date_are_dropped():
    frame = _holders_frame(
        [
            _row("Good"),
            _row(None),  # no holder name
            [None, "No Date", 0.05, 100, 1000, 0.0],  # no reported date
        ]
    )
    ownership = _provider(institutional=frame).get_institutional_ownership("AAPL")
    assert [h.holder for h in ownership.holders] == ["Good"]


def test_nan_numeric_fields_become_none():
    frame = _holders_frame([_row("Fund", pct_change=float("nan"), value=float("nan"))])
    holder = _provider(institutional=frame).get_institutional_ownership("AAPL").holders[0]
    assert holder.pct_change is None
    assert holder.value is None
    assert holder.share_change is None  # can't derive without the percent change


def test_empty_primary_frame_is_no_coverage_not_an_error():
    ownership = _provider(institutional=_holders_frame([])).get_institutional_ownership("ZZZZ")
    assert ownership.is_empty
    assert ownership.breakdown is None


def test_none_primary_frame_is_no_coverage():
    ownership = _provider(institutional=None).get_institutional_ownership("ZZZZ")
    assert ownership.is_empty


def test_mutual_fund_and_major_failures_are_best_effort():
    # The primary feed succeeds; a fund/breakdown failure must not sink it.
    ownership = _provider(
        institutional=_holders_frame([_row("BlackRock")]),
        errors={"mutualfund": RuntimeError("blocked"), "major": RuntimeError("blocked")},
    ).get_institutional_ownership("AAPL")
    assert [h.holder for h in ownership.holders] == ["BlackRock"]
    assert ownership.breakdown is None


def test_primary_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(errors={"institutional": RuntimeError("rate limited")}).get_institutional_ownership("AAPL")


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = YfinanceInstitutionalHoldersProvider(ticker_factory=_boom)
    with pytest.raises(StockDataUnavailable):
        provider.get_institutional_ownership("AAPL")
