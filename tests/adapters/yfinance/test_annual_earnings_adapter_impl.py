from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.adapters.yfinance.annual_earnings_adapter_impl import (
    AnnualEarningsAdapterImpl,
)
from app.domains.shared.exceptions import StockDataUnavailable

_NAN = float("nan")


def _income_stmt(
    periods: list[str],
    *,
    diluted_eps=None,
    basic_eps=None,
    total_revenue=None,
    net_income=None,
    diluted_average_shares=None,
    basic_average_shares=None,
) -> pd.DataFrame:
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
    if diluted_average_shares is not None:
        data["Diluted Average Shares"] = diluted_average_shares
    if basic_average_shares is not None:
        data["Basic Average Shares"] = basic_average_shares
    return pd.DataFrame(data, index=columns).T  # -> rows=metrics, columns=period ends


def _cash_flow(
    periods: list[str],
    *,
    free_cash_flow=None,
    operating_cash_flow=None,
) -> pd.DataFrame:
    columns = pd.DatetimeIndex([pd.Timestamp(p) for p in periods])
    data = {}
    if free_cash_flow is not None:
        data["Free Cash Flow"] = free_cash_flow
    if operating_cash_flow is not None:
        data["Operating Cash Flow"] = operating_cash_flow
    return pd.DataFrame(data, index=columns).T  # -> rows=concepts, columns=period ends


def _estimate_frame(avgs: dict) -> pd.DataFrame:
    return pd.DataFrame.from_dict(
        {label: {"avg": value} for label, value in avgs.items()}, orient="index"
    )


def _earnings_dates(rows: dict[str, float | None]) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [pd.Timestamp(day, tz="America/New_York") for day in rows]
    )
    reported = [_NAN if eps is None else eps for eps in rows.values()]
    return pd.DataFrame({"Reported EPS": reported}, index=index)


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _info(next_fiscal_year_end: date) -> dict:
    return {"nextFiscalYearEnd": _epoch(next_fiscal_year_end)}


class FakeTicker:
    def __init__(
        self,
        *,
        income_stmt=None,
        eps_estimate=None,
        revenue=None,
        info=None,
        earnings_dates=None,
        cashflow=None,
        error=None,
    ):
        self._income_stmt = income_stmt
        self._eps_estimate = eps_estimate
        self._revenue = revenue
        self._info = info if info is not None else {}
        self._earnings_dates = earnings_dates
        self._cashflow = cashflow
        self._error = error
        self.earnings_dates_limits: list[int] = []

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

    @property
    def cashflow(self):
        if self._error is not None:
            raise self._error
        if isinstance(self._cashflow, Exception):  # a selective cash-flow failure
            raise self._cashflow
        return self._cashflow

    def get_earnings_dates(self, limit: int):
        self.earnings_dates_limits.append(limit)
        if isinstance(self._earnings_dates, Exception):  # a selective history failure
            raise self._earnings_dates
        return self._earnings_dates


def provider_with(ticker: FakeTicker) -> AnnualEarningsAdapterImpl:
    return AnnualEarningsAdapterImpl(ticker_factory=lambda _symbol: ticker)


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
        # ~2½ years of quarterly announcements (newest first, like the real frame), one
        # scheduled future row: enough to sum a consensus-basis actual for fy2025 and fy2024,
        # while fy2023's history runs out (two quarters only) and fy2022's is absent.
        earnings_dates=_earnings_dates(
            {
                "2026-04-29": None,  # scheduled, not yet reported
                "2026-01-28": 1.7,  # fy2025 Q4
                "2025-10-30": 1.5,  # fy2025 Q3
                "2025-07-30": 1.5,  # fy2025 Q2
                "2025-04-30": 1.4,  # fy2025 Q1
                "2025-01-30": 1.5,  # fy2024 Q4
                "2024-10-24": 1.4,  # fy2024 Q3
                "2024-07-25": 1.4,  # fy2024 Q2
                "2024-04-25": 1.3,  # fy2024 Q1
                "2024-01-31": 1.3,  # fy2023 Q4
                "2023-10-26": 1.2,  # fy2023 Q3 — fy2023 has only two quarters: no sum
            }
        ),
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


def _ticker_with_cashflow() -> FakeTicker:
    # Two reported years with a cash-flow statement + diluted share counts, so per-share cash
    # and its YoY growth are checkable. fy2025: FCF 60e9 / 20e9 sh = 3.0, OCF 90e9 / 20e9 = 4.5;
    # fy2024: FCF 50e9 / 20e9 = 2.5 → fcf/share growth = (3.0-2.5)/2.5 = 20%.
    return FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31", "2024-12-31"],
            diluted_eps=[6.0, 5.5],
            total_revenue=[400e9, 380e9],
            net_income=[100e9, 95e9],
            diluted_average_shares=[20e9, 20e9],
        ),
        cashflow=_cash_flow(
            ["2025-12-31", "2024-12-31"],
            free_cash_flow=[60e9, 50e9],
            operating_cash_flow=[90e9, 80e9],
        ),
        eps_estimate=_estimate_frame({"0y": 6.5, "+1y": 7.0}),
        revenue=_estimate_frame({"0y": 420e9, "+1y": 450e9}),
        info=_info(date(2026, 12, 31)),
    )


def test_reported_years_carry_cash_flow_per_share():
    tl = provider_with(_ticker_with_cashflow()).get_annual_earnings("AAPL")
    by_year = {y.fiscal_year: y for y in tl.past}
    assert by_year[2025].fcf_per_share == pytest.approx(3.0)  # 60e9 / 20e9 shares
    assert by_year[2025].ocf_per_share == pytest.approx(4.5)  # 90e9 / 20e9 shares
    assert by_year[2024].fcf_per_share == pytest.approx(2.5)  # 50e9 / 20e9 shares


def test_timeline_exposes_latest_cash_flow_and_growth():
    tl = provider_with(_ticker_with_cashflow()).get_annual_earnings("AAPL")
    assert tl.latest_fcf_per_share == pytest.approx(3.0)  # newest reported year
    assert tl.latest_ocf_per_share == pytest.approx(4.5)
    assert tl.latest_fcf_growth_yoy == pytest.approx(20.0)  # (3.0 - 2.5) / 2.5 * 100


def test_blocked_cash_flow_leaves_reported_years_intact_without_cash():
    # cashflow is the same hard-gated fundamentals class as income_stmt: a blocked fetch
    # drops the per-share cash but never sinks the year (best-effort enrichment).
    ticker = _ticker_with_cashflow()
    ticker._cashflow = RuntimeError("cash flow blocked")
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    by_year = {y.fiscal_year: y for y in tl.past}
    assert by_year[2025].eps_actual == 6.0  # the year still serves
    assert by_year[2025].fcf_per_share is None  # cash dropped
    assert tl.latest_fcf_per_share is None
    # a reported year carries actuals only — no estimate side (there's no annual surprise)
    assert by_year[2025].eps_estimate is None and by_year[2025].revenue_estimate is None


def test_reported_years_carry_the_consensus_basis_actual():
    # The sum of the fiscal year's four quarterly "Reported EPS" values — the adjusted basis
    # the forward estimates are quoted on, distinct from the GAAP-diluted eps_actual.
    tl = provider_with(_full_ticker()).get_annual_earnings("AAPL")
    by_year = {y.fiscal_year: y for y in tl.past}
    assert by_year[2025].eps_actual_consensus == 6.1  # 1.4 + 1.5 + 1.5 + 1.7
    assert by_year[2024].eps_actual_consensus == 5.6  # 1.3 + 1.4 + 1.4 + 1.5
    # Where the announcement history runs out, the figure is omitted — never guessed.
    assert by_year[2023].eps_actual_consensus is None  # only two quarters of history
    assert by_year[2022].eps_actual_consensus is None
    # Upcoming years have no actual on any basis.
    assert all(y.eps_actual_consensus is None for y in tl.future)


def test_consensus_actual_requires_all_four_quarters():
    # Three reported quarters inside the fiscal year (one missed row) must not produce a
    # partial-year sum — better no figure than a wrong one.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9], net_income=[100e9]
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
        earnings_dates=_earnings_dates(
            {"2026-01-28": 1.7, "2025-10-30": 1.5, "2025-07-30": 1.5}  # fy2025 Q1 missing
        ),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.past[0].eps_actual_consensus is None


def test_consensus_actual_matches_off_calendar_fiscal_years():
    # A Micron-like August fiscal-year-end: the four announcements from Dec through Sep
    # belong to the fiscal year ending in August — and the prior year's Q4 (announced the
    # September before) must stay outside the window.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-08-28"], diluted_eps=[7.59], total_revenue=[37.4e9], net_income=[8.5e9]
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
        earnings_dates=_earnings_dates(
            {
                "2025-09-23": 3.03,  # fy2025 Q4 (quarter ended late Aug)
                "2025-06-25": 1.91,  # fy2025 Q3
                "2025-03-20": 1.56,  # fy2025 Q2
                "2024-12-18": 1.79,  # fy2025 Q1
                "2024-09-25": 1.18,  # fy2024 Q4 — previous year, must be excluded
            }
        ),
    )
    tl = provider_with(ticker).get_annual_earnings("MU")
    assert tl.past[0].eps_actual_consensus == 8.29  # 1.79 + 1.56 + 1.91 + 3.03


def test_consensus_actual_uses_the_newest_announcement_for_a_restated_quarter():
    # Two announcements landing in the same derived quarter (a restatement/duplicate row):
    # the most recent one wins, and the sum still covers exactly four quarters.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9], net_income=[100e9]
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
        earnings_dates=_earnings_dates(
            {
                "2026-02-10": 1.75,  # fy2025 Q4, restated — newest wins
                "2026-01-28": 1.7,  # fy2025 Q4, original
                "2025-10-30": 1.5,
                "2025-07-30": 1.5,
                "2025-04-30": 1.4,
            }
        ),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.past[0].eps_actual_consensus == 6.15  # 1.4 + 1.5 + 1.5 + 1.75


def test_earnings_dates_failure_degrades_to_no_consensus_actuals():
    # The announcement history is best-effort enrichment: a blocked fetch drops the
    # consensus figures but leaves the reported years (and the rest of the timeline) intact.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9], net_income=[100e9]
        ),
        eps_estimate=_estimate_frame({"0y": 6.5}),
        revenue=_estimate_frame({"0y": 420e9}),
        info=_info(date(2026, 12, 31)),
        earnings_dates=RuntimeError("earnings dates blocked"),
    )
    tl = provider_with(ticker).get_annual_earnings("AAPL")
    assert tl.past[0].eps_actual == 6.0  # reported year intact
    assert tl.past[0].eps_actual_consensus is None
    assert [y.fiscal_year for y in tl.future] == [2026]  # timeline not sunk


def test_announcement_history_is_not_fetched_without_reported_years():
    # Forward-only (income statement blocked): there is no fiscal year to sum for, so the
    # adapter must not spend a Yahoo call on the announcement history.
    ticker = FakeTicker(
        income_stmt=RuntimeError("income statement blocked"),
        eps_estimate=_estimate_frame({"0y": 6.5}),
        revenue=_estimate_frame({"0y": 420e9}),
        info=_info(date(2026, 12, 31)),
        earnings_dates=_earnings_dates({"2026-01-28": 1.7}),
    )
    provider_with(ticker).get_annual_earnings("AAPL")
    assert ticker.earnings_dates_limits == []  # never called


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


# --- foreign ADRs: reporting→trading currency normalization -------------------------------


class _FxTicker:
    def __init__(self, rate):
        self.fast_info = {} if rate is None else {"last_price": rate}


def provider_with_currency(
    ticker: FakeTicker, *, fx_rate
) -> AnnualEarningsAdapterImpl:
    fx_ticker = _FxTicker(fx_rate)

    def factory(symbol):
        return fx_ticker if symbol.endswith("=X") else ticker

    return AnnualEarningsAdapterImpl(ticker_factory=factory)


def _adr_info(next_fiscal_year_end: date, *, financial_currency, forward_eps, currency="USD"):
    return {
        "nextFiscalYearEnd": _epoch(next_fiscal_year_end),
        "currency": currency,
        "financialCurrency": financial_currency,
        "forwardEps": forward_eps,
    }


def test_foreign_adr_converts_reporting_currency_figures_to_trading():
    # TSM-like: income_stmt is in TWD (the reporting currency), the EPS estimate in USD (the
    # trading currency), the revenue estimate in TWD. fx = 1/32 = 0.03125 (TWD→USD).
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"],
            diluted_eps=[320.0],  # TWD → 10.0 USD
            total_revenue=[3.2e12],  # TWD → 1.0e11 USD
            net_income=[8.0e11],  # TWD → 2.5e10 USD
        ),
        eps_estimate=_estimate_frame({"0y": 16.0, "+1y": 20.0}),  # USD — must stay
        revenue=_estimate_frame({"0y": 3.5e12, "+1y": 3.8e12}),  # TWD — must convert
        info=_adr_info(date(2026, 12, 31), financial_currency="TWD", forward_eps=20.0),
    )
    tl = provider_with_currency(ticker, fx_rate=0.03125).get_annual_earnings("TSM")

    reported = tl.past[0]
    assert reported.eps_actual == 10.0  # 320 TWD × 0.03125
    assert reported.revenue_actual == 1.0e11  # 3.2e12 TWD × 0.03125
    assert reported.net_income == 2.5e10  # 8.0e11 TWD × 0.03125

    y0, y1 = tl.future
    # EPS estimate detected as already-trading-currency (USD) → unchanged.
    assert y0.eps_estimate == 16.0 and y1.eps_estimate == 20.0
    # Revenue estimate converted from the reporting currency (TWD → USD).
    assert y0.revenue_estimate == pytest.approx(3.5e12 * 0.03125)
    assert y1.revenue_estimate == pytest.approx(3.8e12 * 0.03125)


def test_foreign_adr_converts_a_reporting_currency_market_eps():
    # BABA-like: the market EPS surfaces (earnings_dates → the consensus actual, and
    # earnings_estimate → the forward EPS) are themselves in the reporting currency (CNY),
    # unlike TSM's. Detected once from the 0y estimate against forwardEps (both USD) and
    # converted, while the GAAP income-statement EPS converts via the reliable rate. fx = 0.15.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[60.0], total_revenue=[1.0e12], net_income=[2.0e11]
        ),
        eps_estimate=_estimate_frame({"0y": 45.0, "+1y": 50.0}),  # CNY — must convert
        revenue=_estimate_frame({"0y": 1.1e12}),  # CNY — must convert
        info=_adr_info(date(2026, 12, 31), financial_currency="CNY", forward_eps=9.0),
        # fy2025's four quarterly Reported EPS (CNY): sum 52.0, on the consensus basis.
        earnings_dates=_earnings_dates(
            {
                "2026-01-28": 16.0,  # fy2025 Q4
                "2025-10-30": 14.0,  # fy2025 Q3
                "2025-07-30": 12.0,  # fy2025 Q2
                "2025-04-30": 10.0,  # fy2025 Q1
            }
        ),
    )
    tl = provider_with_currency(ticker, fx_rate=0.15).get_annual_earnings("BABA")

    reported = tl.past[0]
    assert reported.eps_actual == pytest.approx(60.0 * 0.15)  # 9.0 USD (GAAP, reliable rate)
    # Consensus actual (summed earnings_dates, CNY) converted via the detected market rate.
    assert reported.eps_actual_consensus == pytest.approx(52.0 * 0.15)  # 7.8 USD

    y0, y1 = tl.future
    assert y0.eps_estimate == pytest.approx(45.0 * 0.15)  # 6.75 USD (detected reporting)
    assert y1.eps_estimate == pytest.approx(50.0 * 0.15)  # 7.5 USD
    assert y0.revenue_estimate == pytest.approx(1.1e12 * 0.15)


def test_foreign_adr_with_trading_currency_market_eps_keeps_the_consensus():
    # TSM-like: the market EPS is already trading currency (0y estimate ~ forwardEps), so the
    # consensus actual is left as-is while the income-statement EPS still converts.
    ticker = FakeTicker(
        income_stmt=_income_stmt(["2025-12-31"], diluted_eps=[320.0], total_revenue=[3.2e12]),
        eps_estimate=_estimate_frame({"0y": 16.0, "+1y": 20.0}),  # USD
        revenue=_estimate_frame({"0y": 3.5e12}),
        info=_adr_info(date(2026, 12, 31), financial_currency="TWD", forward_eps=20.0),
        earnings_dates=_earnings_dates(
            {  # already-USD quarterly Reported EPS, sum 10.0 — must not be converted
                "2026-01-28": 3.0,
                "2025-10-30": 2.5,
                "2025-07-30": 2.3,
                "2025-04-30": 2.2,
            }
        ),
    )
    tl = provider_with_currency(ticker, fx_rate=0.03125).get_annual_earnings("TSM")
    reported = tl.past[0]
    assert reported.eps_actual == pytest.approx(320.0 * 0.03125)  # 10.0 USD (converted)
    assert reported.eps_actual_consensus == pytest.approx(10.0)  # left as-is (already USD)


def test_foreign_adr_without_an_fx_rate_leaves_figures_unconverted():
    # The FX pair yields no rate: fall back to the identity normalizer (never-worse) rather
    # than a wrong conversion — the reporting-currency figures pass through unchanged.
    ticker = FakeTicker(
        income_stmt=_income_stmt(["2025-12-31"], diluted_eps=[320.0], total_revenue=[3.2e12]),
        eps_estimate=_estimate_frame({"0y": 16.0}),
        revenue=_estimate_frame({"0y": 3.5e12}),
        info=_adr_info(date(2026, 12, 31), financial_currency="TWD", forward_eps=20.0),
    )
    tl = provider_with_currency(ticker, fx_rate=None).get_annual_earnings("TSM")
    assert tl.past[0].eps_actual == 320.0  # unconverted
    assert tl.future[0].revenue_estimate == 3.5e12  # unconverted


def test_domestic_issuer_is_not_converted_and_makes_no_fx_call():
    # currency == financialCurrency (a US company): the normalizer short-circuits to the
    # identity without ever fetching an FX pair.
    ticker = FakeTicker(
        income_stmt=_income_stmt(
            ["2025-12-31"], diluted_eps=[6.0], total_revenue=[400e9], net_income=[100e9]
        ),
        eps_estimate=_estimate_frame({"0y": 6.5}),
        revenue=_estimate_frame({"0y": 420e9}),
        info=_adr_info(date(2026, 12, 31), financial_currency="USD", forward_eps=6.5),
    )

    def factory(symbol):
        if symbol.endswith("=X"):
            raise AssertionError("a domestic issuer must not fetch an FX rate")
        return ticker

    tl = AnnualEarningsAdapterImpl(ticker_factory=factory).get_annual_earnings("AAPL")
    assert tl.past[0].eps_actual == 6.0  # unchanged
    assert tl.future[0].revenue_estimate == 420e9  # unchanged
