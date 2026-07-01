"""Unit tests for the yfinance annual-earnings adapter.

No network: a fake Ticker returns the pandas frames yfinance would, so this checks the
mapping — the reported years (Diluted/Basic EPS + Total Revenue + Net Income) from
``income_stmt``, and the upcoming years (at most two: ``0y`` / ``+1y``) from the
``earnings_estimate`` / ``revenue_estimate`` frames, labelled by the fiscal-year-end from
``info``. Also: the chronological ordering, the calendar/off-calendar fiscal-year derivation,
the income-statement failure degrading to a forward-only timeline (the production case where
Yahoo IP-gates the fundamentals endpoint), an uncovered symbol degrading to an empty timeline,
and any vendor failure on the primary surfaces becoming a domain error.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_annual_earnings_adapter import (
    YfinanceAnnualEarningsProvider,
)
from app.stocks.exceptions import StockDataUnavailable

_NAN = float("nan")


def _income_stmt(
    periods: list[str],
    *,
    diluted_eps=None,
    basic_eps=None,
    total_revenue=None,
    net_income=None,
) -> pd.DataFrame:
    """An annual income statement like ``Ticker.income_stmt``: metrics as rows, fiscal-year-end
    dates as columns. Each metric arg is a list aligned to ``periods``."""
    columns = pd.DatetimeIndex([pd.Timestamp(p) for p in periods])
    data = {}
    if diluted_eps is not None:
        data["Diluted EPS"] = diluted_eps
    if basic_eps is not None:
        data["Basic EPS"] = basic_eps
    if total_revenue is not None:
        data["Total Revenue"] = total_revenue
    if net_income is not None:
        data["Net Income"] = net_income
    return pd.DataFrame(data, index=columns).T  # -> rows=metrics, columns=period ends


def _estimate_frame(avgs: dict) -> pd.DataFrame:
    """A period-indexed estimate frame like ``earnings_estimate`` / ``revenue_estimate``:
    ``{"0y": 6.5, "+1y": 7.0}`` → rows keyed by period with an ``avg`` column."""
    return pd.DataFrame.from_dict(
        {label: {"avg": value} for label, value in avgs.items()}, orient="index"
    )


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _info(next_fiscal_year_end: date) -> dict:
    """``Ticker.info`` carrying just the ``nextFiscalYearEnd`` the adapter anchors ``0y`` on."""
    return {"nextFiscalYearEnd": _epoch(next_fiscal_year_end)}


class FakeTicker:
    """Stands in for ``yfinance.Ticker``; serves canned frames, or raises."""

    def __init__(
        self, *, income_stmt=None, eps_estimate=None, revenue=None, info=None, error=None
    ):
        self._income_stmt = income_stmt
        self._eps_estimate = eps_estimate
        self._revenue = revenue
        self._info = info if info is not None else {}
        self._error = error

    @property
    def earnings_estimate(self):
        if self._error is not None:
            raise self._error
        return self._eps_estimate

    @property
    def revenue_estimate(self):
        if self._error is not None:
            raise self._error
        return self._revenue

    @property
    def income_stmt(self):
        if self._error is not None:
            raise self._error
        if isinstance(self._income_stmt, Exception):  # a selective income-stmt failure
            raise self._income_stmt
        return self._income_stmt

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


def provider_with(ticker: FakeTicker) -> YfinanceAnnualEarningsProvider:
    return YfinanceAnnualEarningsProvider(ticker_factory=lambda _symbol: ticker)


# Five reported fiscal years (calendar year-ends) so the four-most-recent cap is exercised;
# 0q/+1q live on the estimate frames too and must be ignored (annual reads only 0y/+1y).
_PERIODS = ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31"]


def _full_ticker() -> FakeTicker:
    return FakeTicker(
        income_stmt=_income_stmt(
            _PERIODS,
            diluted_eps=[6.0, 5.5, 5.0, 4.5, 4.0],
            total_revenue=[400e9, 380e9, 360e9, 340e9, 320e9],
            net_income=[100e9, 95e9, 90e9, 85e9, 80e9],
        ),
        eps_estimate=_estimate_frame({"0q": 1.6, "+1q": 1.7, "0y": 6.5, "+1y": 7.0}),
        revenue=_estimate_frame({"0q": 100e9, "+1q": 110e9, "0y": 420e9, "+1y": 450e9}),
        info=_info(date(2026, 12, 31)),  # 0y ends 2026-12-31 → fy2026; +1y → fy2027
    )


def test_keeps_the_four_most_recent_reported_years():
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    # Past runs oldest→newest; the 5th-oldest (fy2021) is dropped, keeping four.
    assert [y.fiscal_year for y in tl.past] == [2022, 2023, 2024, 2025]
    assert all(y.fiscal_year != 2021 for y in tl.years)  # oldest reported dropped


def test_reported_years_carry_actuals():
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    by_year = {y.fiscal_year: y for y in tl.past}
    assert by_year[2025].eps_actual == 6.0
    assert by_year[2025].revenue_actual == 400e9
    assert by_year[2025].net_income == 100e9
    assert by_year[2025].period_end == date(2025, 12, 31)
    # a reported year carries actuals only — no estimate side (there's no annual surprise)
    assert by_year[2025].eps_estimate is None and by_year[2025].revenue_estimate is None


def test_years_run_chronologically_past_then_upcoming():
    # The whole timeline reads oldest→newest: the four reported years ascending, then the two
    # upcoming ones — the single chronological order the read endpoint serves.
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    assert [y.fiscal_year for y in tl.years] == [2022, 2023, 2024, 2025, 2026, 2027]
    # is_reported flips exactly once, at the past→upcoming boundary (no interleaving).
    assert [y.is_reported for y in tl.years] == [True, True, True, True, False, False]


def test_upcoming_is_the_two_forward_estimate_years():
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    assert [y.fiscal_year for y in tl.future] == [2026, 2027]

    y0, y1 = tl.future
    # 0y: EPS + revenue from the estimate frames, labelled by info's nextFiscalYearEnd.
    assert y0.eps_estimate == 6.5 and y0.revenue_estimate == 420e9
    assert y0.period_end == date(2026, 12, 31)
    assert y0.eps_actual is None and y0.revenue_actual is None and y0.is_reported is False
    # +1y: a year on from 0y.
    assert y1.eps_estimate == 7.0 and y1.revenue_estimate == 450e9
    assert y1.period_end == date(2027, 12, 31)


def test_ignores_the_quarterly_estimate_rows():
    # 0q/+1q are present on the estimate frames but must be ignored — annual reads only 0y/+1y.
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    assert len(tl.future) == 2 and [y.fiscal_year for y in tl.future] == [2026, 2027]


def test_upcoming_falls_back_to_reported_year_when_info_is_missing():
    # No nextFiscalYearEnd in info → anchor 0y one year past the latest reported year.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9], net_income=[100e9]
        ),
        eps_estimate=_estimate_frame({"0y": 6.5, "+1y": 7.0}),
        revenue=_estimate_frame({"0y": 420e9, "+1y": 450e9}),
        info={},  # no nextFiscalYearEnd
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert [y.fiscal_year for y in tl.future] == [2026, 2027]


def test_only_one_upcoming_when_plus1y_has_no_estimate():
    ticker = FakeTicker(
        income_stmt=_income_stmt(["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9]),
        eps_estimate=_estimate_frame({"0y": 6.5}),
        revenue=_estimate_frame({"0y": 420e9}),
        info=_info(date(2026, 12, 31)),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert [y.fiscal_year for y in tl.future] == [2026]
    assert tl.future[0].eps_estimate == 6.5


def test_no_upcoming_when_estimates_are_empty():
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31", "2024-12-31"], diluted_eps=[6.0, 5.5], total_revenue=[400e9, 380e9]
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
        info=_info(date(2026, 12, 31)),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.future == ()
    assert [y.fiscal_year for y in tl.past] == [2024, 2025]


def test_income_statement_failure_degrades_to_forward_only():
    # The production case: Yahoo IP-gates the fundamentals endpoint from the data-centre, so
    # income_stmt fails while the estimate frames (not gated) still serve. The reported years
    # drop out, but the forward years must still populate — the timeline is not sunk.
    ticker = FakeTicker(
        income_stmt=RuntimeError("income statement blocked"),
        eps_estimate=_estimate_frame({"0y": 6.5, "+1y": 7.0}),
        revenue=_estimate_frame({"0y": 420e9, "+1y": 450e9}),
        info=_info(date(2026, 12, 31)),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.past == ()  # reported dropped
    assert [y.fiscal_year for y in tl.future] == [2026, 2027]  # forward still serves


def test_basic_eps_is_used_when_diluted_is_absent():
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], basic_eps=[6.1], total_revenue=[400e9], net_income=[100e9]
        ),  # no Diluted EPS row
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.past[0].eps_actual == 6.1


def test_reported_column_without_eps_is_skipped():
    # A column with revenue/net income but no EPS at all is skipped — without a reported EPS it
    # couldn't be told apart from an upcoming year (the is_reported discriminator).
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31", "2024-12-31"],
            diluted_eps=[6.0, _NAN],  # fy2024 has no EPS
            total_revenue=[400e9, 380e9],
            net_income=[100e9, 95e9],
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert [y.fiscal_year for y in tl.past] == [2025]  # fy2024 skipped (no EPS)


def test_off_calendar_fiscal_year_labels_come_from_the_period_end():
    # An August fiscal-year-end (Micron-like): the reported year's label is the period-end's
    # calendar year, and the forward years follow info's August nextFiscalYearEnd.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-08-31", "2024-08-31"],
            diluted_eps=[7.59, 0.70],
            total_revenue=[37.4e9, 25.1e9],
            net_income=[8.5e9, 0.78e9],
        ),
        eps_estimate=_estimate_frame({"0y": 73.0, "+1y": 149.0}),
        revenue=_estimate_frame({"0y": 129e9, "+1y": 235e9}),
        info=_info(date(2026, 8, 31)),
    )
    tl = provider_with(ticker).get_annual_earnings("MU")
    past = {y.fiscal_year: y for y in tl.past}
    assert set(past) == {2024, 2025}
    assert past[2025].period_end == date(2025, 8, 31)
    assert [y.fiscal_year for y in tl.future] == [2026, 2027]
    assert tl.future[0].period_end == date(2026, 8, 31)


def test_empty_frames_yield_empty_timeline():
    ticker = FakeTicker(
        income_stmt=_income_stmt([]),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    assert provider_with(ticker).get_annual_earnings("ZZZZ").is_empty


def test_none_frames_yield_empty_timeline():
    assert provider_with(FakeTicker()).get_annual_earnings("ZZZZ").is_empty


def test_vendor_error_raises_unavailable():
    ticker = FakeTicker(error=RuntimeError("yahoo down"))
    with pytest.raises(StockDataUnavailable):
        provider_with(ticker).get_annual_earnings("AAPL")
