"""Unit tests for the yfinance quarterly-earnings adapter.

No network: a fake Ticker returns the pandas frames yfinance would, so this checks the
mapping — the past quarters (reported EPS + a surprise computed from actual vs. estimate)
from ``earnings_dates``, and the upcoming quarters (at most two: ``0q`` / ``+1q``) from the
``earnings_estimate`` / ``revenue_estimate`` frames, with a scheduled report date attached
when one lines up. Also: the calendar fiscal-period derivation, an uncovered symbol degrading
to an empty timeline, and any vendor failure becoming a domain error.
"""

from datetime import date

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_quarterly_earnings_adapter import (
    YfinanceQuarterlyEarningsProvider,
)
from app.stocks.exceptions import StockDataUnavailable

_NAN = float("nan")


def _earnings_dates(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """A date-indexed frame like ``Ticker.earnings_dates``: rows of
    ``(announce_date, EPS Estimate, Reported EPS)``; a NaN Reported EPS is a future date."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _ in rows])
    return pd.DataFrame(
        {
            "EPS Estimate": [est for _, est, _ in rows],
            "Reported EPS": [rep for _, _, rep in rows],
        },
        index=index,
    )


def _estimate_frame(avgs: dict) -> pd.DataFrame:
    """A period-indexed estimate frame like ``earnings_estimate`` / ``revenue_estimate``:
    ``{"0q": 3.1, "+1q": 3.3}`` → rows keyed by period with an ``avg`` column."""
    return pd.DataFrame.from_dict(
        {label: {"avg": value} for label, value in avgs.items()}, orient="index"
    )


def _income_stmt(revenue_by_date: dict) -> pd.DataFrame:
    """A date-columned income statement like ``quarterly_income_stmt``:
    ``{"2025-12-31": 5e9}`` → a frame with a ``Total Revenue`` row over period-end columns."""
    columns = pd.DatetimeIndex([pd.Timestamp(d) for d in revenue_by_date])
    return pd.DataFrame(
        [list(revenue_by_date.values())], index=["Total Revenue"], columns=columns
    )


class FakeTicker:
    """Stands in for ``yfinance.Ticker``; serves canned frames, or raises."""

    def __init__(
        self, *, earnings_dates=None, eps_estimate=None, revenue=None, income_stmt=None, error=None
    ):
        self._earnings_dates = earnings_dates
        self._eps_estimate = eps_estimate
        self._revenue = revenue
        self._income_stmt = income_stmt
        self._error = error

    @property
    def earnings_dates(self):
        if self._error is not None:
            raise self._error
        return self._earnings_dates

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
    def quarterly_income_stmt(self):
        if self._error is not None:
            raise self._error
        if isinstance(self._income_stmt, Exception):  # a selective income-stmt failure
            raise self._income_stmt
        return self._income_stmt


def provider_with(ticker: FakeTicker) -> YfinanceQuarterlyEarningsProvider:
    return YfinanceQuarterlyEarningsProvider(ticker_factory=lambda _symbol: ticker)


def _reported_dates() -> list[tuple[str, float, float]]:
    return [
        ("2025-02-01", 2.7, 2.9),  # fy2024 q4 — oldest, dropped (5 reported, keep 4)
        ("2025-05-01", 2.4, 2.5),  # fy2025 q1
        ("2025-08-01", 2.6, 2.5),  # fy2025 q2 — a miss (actual < estimate)
        ("2025-11-01", 2.8, 3.0),  # fy2025 q3
        ("2026-02-01", 3.0, 3.3),  # fy2025 q4 — newest reported
    ]


def _full_ticker(future=(("2026-05-01", 3.1, _NAN),)) -> FakeTicker:
    # One scheduled future date by default (the common case, like SNDK), yet both 0q/+1q
    # upcoming quarters should still surface from the estimate frames.
    dates = _earnings_dates(_reported_dates() + list(future))
    return FakeTicker(
        earnings_dates=dates,
        eps_estimate=_estimate_frame({"0q": 3.1, "+1q": 3.3, "0y": 12.0, "+1y": 14.0}),
        revenue=_estimate_frame({"0q": 100e9, "+1q": 110e9, "0y": 420e9, "+1y": 455e9}),
        income_stmt=_income_stmt(
            {  # reported revenue by fiscal period end (calendar quarters here)
                "2025-12-31": 5.0e9,  # fy2025 q4
                "2025-09-30": 4.0e9,  # fy2025 q3
                "2025-06-30": 3.0e9,  # fy2025 q2
                "2025-03-31": 2.0e9,  # fy2025 q1
            }
        ),
    )


def test_keeps_the_four_most_recent_reported_quarters():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    # Past runs oldest→newest; the 5th-oldest (fy2024 q4) is dropped, keeping four.
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.past] == [
        (2025, 1),
        (2025, 2),
        (2025, 3),
        (2025, 4),
    ]
    assert all(q.fiscal_year != 2024 for q in tl.quarters)  # oldest reported dropped


def test_quarters_run_chronologically_past_then_upcoming():
    # The whole timeline reads oldest→newest: the four reported quarters ascending, then
    # the two upcoming ones — the single chronological order the read endpoint serves.
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.quarters] == [
        (2025, 1),
        (2025, 2),
        (2025, 3),
        (2025, 4),
        (2026, 1),
        (2026, 2),
    ]
    # is_reported flips exactly once, at the past→upcoming boundary (no interleaving).
    assert [q.is_reported for q in tl.quarters] == [True, True, True, True, False, False]


def test_upcoming_is_the_two_forward_estimate_quarters():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.future] == [(2026, 1), (2026, 2)]

    q0, q1 = tl.future
    # 0q: EPS + revenue from the estimate frames, date from earnings_dates, no actual.
    assert q0.eps_estimate == 3.1 and q0.revenue_estimate == 100e9
    assert q0.report_date == date(2026, 5, 1)
    assert q0.eps_actual is None and q0.is_reported is False
    # +1q: estimates present, but no scheduled date (Yahoo only lists the nearest one).
    assert q1.eps_estimate == 3.3 and q1.revenue_estimate == 110e9
    assert q1.report_date is None


def test_at_most_two_upcoming_even_with_more_future_dates():
    # Three scheduled future dates, but only 0q/+1q are estimated → still two upcoming.
    ticker = _full_ticker(
        future=(
            ("2026-05-01", 3.1, _NAN),
            ("2026-08-01", 3.3, _NAN),
            ("2026-11-01", 3.5, _NAN),
        )
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    assert len(tl.future) == 2
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.future] == [(2026, 1), (2026, 2)]


def test_upcoming_surfaces_without_any_scheduled_future_date():
    # No future date at all (only reported rows): the pair is anchored one quarter past the
    # latest reported quarter, dateless, straight from the estimate frames.
    ticker = FakeTicker(
        earnings_dates=_earnings_dates(_reported_dates()),
        eps_estimate=_estimate_frame({"0q": 3.1, "+1q": 3.3}),
        revenue=_estimate_frame({"0q": 100e9, "+1q": 110e9}),
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.future] == [(2026, 1), (2026, 2)]
    assert all(q.report_date is None for q in tl.future)
    assert tl.future[0].eps_estimate == 3.1 and tl.future[1].revenue_estimate == 110e9


def test_only_one_upcoming_when_plus1q_has_no_estimate():
    # 0q estimated, +1q absent → a single upcoming quarter ("if it is available").
    ticker = FakeTicker(
        earnings_dates=_earnings_dates(
            _reported_dates() + [("2026-05-01", 3.1, _NAN)]
        ),
        eps_estimate=_estimate_frame({"0q": 3.1}),
        revenue=_estimate_frame({"0q": 100e9}),
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.future] == [(2026, 1)]
    assert tl.future[0].eps_estimate == 3.1


def test_no_upcoming_when_estimates_are_empty():
    # Reported history but no forward estimates and no future date → past only, no upcoming.
    ticker = FakeTicker(
        earnings_dates=_earnings_dates(_reported_dates()),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    assert tl.future == ()
    assert len(tl.past) == 4


def test_computes_the_surprise_from_actual_and_estimate():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    q4 = next(q for q in tl.past if q.fiscal_quarter == 4)  # fy2025 q4: est 3.0, actual 3.3
    assert q4.eps_actual == 3.3 and q4.eps_estimate == 3.0
    assert q4.eps_surprise == 0.3 and q4.eps_surprise_percent == 10.0
    assert q4.beat is True

    q2 = next(q for q in tl.past if q.fiscal_quarter == 2)  # a miss: 2.5 vs 2.6
    assert q2.eps_surprise == -0.1 and q2.eps_surprise_percent == -3.85
    assert q2.beat is False


def test_derives_period_end_and_fiscal_labels_from_the_announcement():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    q1 = next(q for q in tl.past if (q.fiscal_year, q.fiscal_quarter) == (2025, 1))
    assert q1.report_date == date(2025, 5, 1)
    assert q1.period_end == date(2025, 3, 31)  # most recent quarter-end before the report


def test_reported_quarters_carry_revenue_actual():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    by_quarter = {(q.fiscal_year, q.fiscal_quarter): q for q in tl.past}
    # matched from the income statement column most recently preceding the announcement
    assert by_quarter[(2025, 4)].revenue_actual == 5.0e9
    assert by_quarter[(2025, 3)].revenue_actual == 4.0e9
    assert by_quarter[(2025, 1)].revenue_actual == 2.0e9
    # upcoming quarters carry the estimate, never a reported actual
    assert all(q.revenue_actual is None for q in tl.future)


def _off_calendar_ticker(income_stmt) -> FakeTicker:
    """An MU-like off-calendar filer: fiscal quarters ending late Feb/May/Aug/Nov, each
    announced ~4 weeks later (late Mar/Jun/Sep/Dec). The calendar-derived label therefore
    names the *previous* calendar quarter (e.g. the May-ended quarter, announced late June,
    is labelled Q1 ending Mar 31)."""
    return FakeTicker(
        earnings_dates=_earnings_dates(
            [
                ("2025-09-24", 2.5, 2.8),  # quarter ended 2025-08-28 → label (2025, 2)
                ("2025-12-18", 2.9, 3.0),  # quarter ended 2025-11-27 → label (2025, 3)
                ("2026-03-20", 3.1, 3.2),  # quarter ended 2026-02-26 → label (2025, 4)
                ("2026-06-26", 3.4, 3.5),  # quarter ended 2026-05-28 → label (2026, 1)
            ]
        ),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
        income_stmt=income_stmt,
    )


def test_off_calendar_filer_pairs_revenue_with_the_eps_quarter():
    # Regression: the income statement used to be keyed by the calendar label's
    # year+quarter, which for an off-calendar filer picks the PREVIOUS fiscal quarter's
    # column (the newest row here, labelled 2026 Q1, would take the 2026-02-26 revenue
    # alongside the May-quarter EPS). Matching by true period proximity keeps each row's
    # EPS and revenue on the same fiscal quarter, even though the label stays offset.
    ticker = _off_calendar_ticker(
        _income_stmt(
            {  # true fiscal period ends, ~a month before each announcement
                "2026-05-28": 9.30e9,
                "2026-02-26": 8.05e9,
                "2025-11-27": 8.71e9,
                "2025-08-28": 7.75e9,
            }
        )
    )
    tl = provider_with(ticker).get_quarterly_earnings("MU")
    by_report = {q.report_date: q for q in tl.past}
    assert by_report[date(2026, 6, 26)].revenue_actual == 9.30e9  # not 8.05e9 (prev quarter)
    assert by_report[date(2026, 3, 20)].revenue_actual == 8.05e9
    assert by_report[date(2025, 12, 18)].revenue_actual == 8.71e9
    assert by_report[date(2025, 9, 24)].revenue_actual == 7.75e9


def test_stale_income_statement_drops_revenue_rather_than_misattaching():
    # The income statement hasn't published the just-announced quarter yet: the nearest
    # preceding column is the previous quarter, ~4 months before the announcement — the
    # newest row must carry no revenue rather than the wrong quarter's figure.
    ticker = _off_calendar_ticker(
        _income_stmt(
            {
                "2026-02-26": 8.05e9,
                "2025-11-27": 8.71e9,
                "2025-08-28": 7.75e9,
            }
        )
    )
    tl = provider_with(ticker).get_quarterly_earnings("MU")
    by_report = {q.report_date: q for q in tl.past}
    assert by_report[date(2026, 6, 26)].revenue_actual is None  # not 8.05e9
    assert by_report[date(2026, 3, 20)].revenue_actual == 8.05e9  # older rows still match


def test_income_statement_failure_degrades_gracefully():
    # A blocked/failed income statement drops revenue_actual but must not sink the timeline.
    ticker = FakeTicker(
        earnings_dates=_earnings_dates(_reported_dates()),
        eps_estimate=_estimate_frame({"0q": 3.1, "+1q": 3.3}),
        revenue=_estimate_frame({"0q": 100e9, "+1q": 110e9}),
        income_stmt=RuntimeError("income statement blocked"),
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    assert len(tl.past) == 4  # timeline intact
    assert all(q.revenue_actual is None for q in tl.past)  # just no reported revenue


def test_reported_without_an_estimate_has_no_surprise():
    ticker = FakeTicker(
        earnings_dates=_earnings_dates([("2025-05-01", _NAN, 2.5)]),  # reported, no estimate
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    tl = provider_with(ticker).get_quarterly_earnings("AAPL")
    q = tl.past[0]
    assert q.eps_actual == 2.5 and q.eps_estimate is None
    assert q.eps_surprise is None and q.eps_surprise_percent is None
    assert q.beat is None


def test_empty_frames_yield_empty_timeline():
    ticker = FakeTicker(
        earnings_dates=_earnings_dates([]),
        eps_estimate=_estimate_frame({}),
        revenue=_estimate_frame({}),
    )
    assert provider_with(ticker).get_quarterly_earnings("ZZZZ").is_empty


def test_none_frames_yield_empty_timeline():
    assert provider_with(FakeTicker()).get_quarterly_earnings("ZZZZ").is_empty


def test_vendor_error_raises_unavailable():
    ticker = FakeTicker(error=RuntimeError("yahoo down"))
    with pytest.raises(StockDataUnavailable):
        provider_with(ticker).get_quarterly_earnings("AAPL")
