"""Unit tests for the yfinance analyst-estimates adapter.

No network: a fake Ticker returns the pandas frames yfinance would, so this checks the
mapping — current fiscal year (``0y``) → FY1 and next (``+1y``) → FY2, the fiscal-year
labels derived from ``info['nextFiscalYearEnd']``, the forward series that backs the
growth math, NaN/missing cells collapsing to ``None``, an uncovered symbol degrading to
an empty estimate, and any vendor failure becoming a domain error.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.adapters.yfinance_estimates_adapter import YfinanceEstimatesProvider


def _epoch(d: date) -> int:
    """The Unix timestamp yfinance would report for a fiscal-year-end date."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _frame(rows: dict) -> pd.DataFrame:
    """A period-indexed estimate frame, like ``Ticker.earnings_estimate``."""
    return pd.DataFrame.from_dict(rows, orient="index")


class FakeTicker:
    """Stands in for ``yfinance.Ticker``; serves canned frames + info, or raises."""

    def __init__(self, *, earnings=None, revenue=None, info=None, error=None):
        self._earnings = earnings
        self._revenue = revenue
        self._info = {} if info is None else info
        self._error = error

    @property
    def earnings_estimate(self):
        if self._error is not None:
            raise self._error
        return self._earnings

    @property
    def revenue_estimate(self):
        if self._error is not None:
            raise self._error
        return self._revenue

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


def provider_with(ticker: FakeTicker) -> YfinanceEstimatesProvider:
    return YfinanceEstimatesProvider(ticker_factory=lambda _symbol: ticker)


def _eps_frame() -> pd.DataFrame:
    return _frame(
        {
            "0q": {"avg": 1.9, "low": 1.8, "high": 2.0, "numberOfAnalysts": 25},
            "+1q": {"avg": 2.1, "low": 1.9, "high": 2.3, "numberOfAnalysts": 24},
            "0y": {"avg": 8.0, "low": 7.0, "high": 9.0, "numberOfAnalysts": 30},
            "+1y": {"avg": 9.2, "low": 8.1, "high": 10.4, "numberOfAnalysts": 28},
        }
    )


def _revenue_frame() -> pd.DataFrame:
    return _frame(
        {
            "0q": {"avg": 100e9, "low": 98e9, "high": 103e9, "numberOfAnalysts": 24},
            "+1q": {"avg": 110e9, "low": 105e9, "high": 118e9, "numberOfAnalysts": 23},
            "0y": {"avg": 420e9, "low": 410e9, "high": 430e9, "numberOfAnalysts": 27},
            "+1y": {"avg": 455e9, "low": 440e9, "high": 470e9, "numberOfAnalysts": 26},
        }
    )


def _full_ticker() -> FakeTicker:
    return FakeTicker(
        earnings=_eps_frame(),
        revenue=_revenue_frame(),
        info={"nextFiscalYearEnd": _epoch(date(2027, 1, 31))},
    )


def test_maps_current_year_to_fy1_and_next_to_fy2():
    est = provider_with(_full_ticker()).get_estimates("AAPL")
    assert isinstance(est, AnalystEstimates)
    # FY1 = the current (in-progress) fiscal year, labelled from nextFiscalYearEnd.
    assert est.fiscal_year == 2027
    assert est.period_end == date(2027, 1, 31)
    assert est.eps_avg == 8.0
    assert est.eps_low == 7.0
    assert est.eps_high == 9.0
    assert est.revenue_avg == 420e9
    assert est.num_analysts_eps == 30
    assert est.num_analysts_revenue == 27
    # FY2 = the year after.
    assert est.eps_avg_fy2 == 9.2
    assert est.fiscal_year_fy2 == 2028


def test_builds_forward_series_for_growth():
    est = provider_with(_full_ticker()).get_estimates("AAPL")
    assert [(y.fiscal_year, y.eps_avg, y.revenue_avg) for y in est.forward_years] == [
        (2027, 8.0, 420e9),
        (2028, 9.2, 455e9),
    ]
    assert est.forward_eps_growth() == 15.0  # 9.2 / 8.0 - 1
    assert est.forward_revenue_growth() == 8.33  # 455 / 420 - 1


def test_forward_pe_and_ps_from_returned_estimates():
    est = provider_with(_full_ticker()).get_estimates("AAPL")
    assert est.forward_pe(280.0) == 35.0  # 280 / 8.0
    assert est.forward_ps(2_100e9) == 5.0  # 2.1T / 420B


def test_uncovered_symbol_yields_empty_estimates():
    # Yahoo returns empty frames for a symbol it doesn't cover.
    ticker = FakeTicker(earnings=_frame({}), revenue=_frame({}), info={})
    est = provider_with(ticker).get_estimates("ZZZZ")
    assert est.is_empty


def test_none_frames_yield_empty_estimates():
    est = provider_with(FakeTicker()).get_estimates("ZZZZ")
    assert est.is_empty


def test_nan_and_missing_cells_become_none():
    eps = _frame({"0y": {"avg": 8.0}, "+1y": {"avg": float("nan")}})  # no low/high cols
    revenue = _frame({"0y": {"avg": float("nan")}})  # FY1 revenue absent
    ticker = FakeTicker(
        earnings=eps,
        revenue=revenue,
        info={"nextFiscalYearEnd": _epoch(date(2027, 1, 31))},
    )
    est = provider_with(ticker).get_estimates("AAPL")
    assert est.eps_avg == 8.0
    assert est.eps_low is None and est.eps_high is None
    assert est.revenue_avg is None
    assert est.num_analysts_eps is None
    assert est.eps_avg_fy2 is None  # +1y avg was NaN


def test_missing_fiscal_year_end_still_returns_estimates_without_labels():
    # No nextFiscalYearEnd → estimates serve, but with no fiscal-year label and hence
    # no forward series (a series row needs a period end).
    ticker = FakeTicker(earnings=_eps_frame(), revenue=_revenue_frame(), info={})
    est = provider_with(ticker).get_estimates("AAPL")
    assert est.eps_avg == 8.0
    assert est.fiscal_year is None
    assert est.period_end is None
    assert est.forward_years == ()


def test_info_failure_does_not_sink_the_estimate():
    # info raising (rate-limited) must not fail the estimate — just drop the dates.
    ticker = FakeTicker(
        earnings=_eps_frame(),
        revenue=_revenue_frame(),
        info=RuntimeError("rate limited"),
    )
    est = provider_with(ticker).get_estimates("AAPL")
    assert est.eps_avg == 8.0
    assert est.fiscal_year is None


def test_vendor_error_raises_unavailable():
    ticker = FakeTicker(error=RuntimeError("yahoo down"))
    with pytest.raises(StockDataUnavailable):
        provider_with(ticker).get_estimates("AAPL")
