"""Unit tests for the yfinance quarterly-earnings adapter.

No network: a fake Ticker returns the pandas frames yfinance would, so this checks the
mapping — reported vs. upcoming split on a missing Reported EPS, the 4-past/4-future
windowing, the surprise computed from actual vs. estimate, forward revenue attached to the
nearest upcoming quarters, the calendar fiscal-period derivation from the announcement
date, an uncovered symbol degrading to an empty timeline, and any vendor failure becoming a
domain error.
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
    ``(announce_date, EPS Estimate, Reported EPS)``; a NaN Reported EPS is an upcoming
    quarter."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _ in rows])
    return pd.DataFrame(
        {
            "EPS Estimate": [est for _, est, _ in rows],
            "Reported EPS": [rep for _, _, rep in rows],
        },
        index=index,
    )


def _revenue_estimate(rows: dict | None = None) -> pd.DataFrame:
    rows = rows or {"0q": 100e9, "+1q": 110e9, "0y": 420e9, "+1y": 455e9}
    return pd.DataFrame.from_dict(
        {label: {"avg": value} for label, value in rows.items()}, orient="index"
    )


class FakeTicker:
    """Stands in for ``yfinance.Ticker``; serves canned frames, or raises."""

    def __init__(self, *, earnings_dates=None, revenue=None, error=None):
        self._earnings_dates = earnings_dates
        self._revenue = revenue
        self._error = error

    @property
    def earnings_dates(self):
        if self._error is not None:
            raise self._error
        return self._earnings_dates

    @property
    def revenue_estimate(self):
        if self._error is not None:
            raise self._error
        return self._revenue


def provider_with(ticker: FakeTicker) -> YfinanceQuarterlyEarningsProvider:
    return YfinanceQuarterlyEarningsProvider(ticker_factory=lambda _symbol: ticker)


def _full_ticker() -> FakeTicker:
    dates = _earnings_dates(
        [
            # reported (Reported EPS present) — 5 rows, newest 4 kept
            ("2025-02-01", 2.7, 2.9),  # fy2024 q4 — oldest, dropped
            ("2025-05-01", 2.4, 2.5),  # fy2025 q1
            ("2025-08-01", 2.6, 2.5),  # fy2025 q2 — a miss (actual < estimate)
            ("2025-11-01", 2.8, 3.0),  # fy2025 q3
            ("2026-02-01", 3.0, 3.3),  # fy2025 q4 — newest
            # upcoming (Reported EPS NaN) — 5 rows, soonest 4 kept
            ("2026-05-01", 3.1, _NAN),  # fy2026 q1 (0q → revenue 100e9)
            ("2026-08-01", 3.3, _NAN),  # fy2026 q2 (+1q → revenue 110e9)
            ("2026-11-01", 3.5, _NAN),  # fy2026 q3 (no forward revenue)
            ("2027-02-01", 3.7, _NAN),  # fy2026 q4 (no forward revenue)
            ("2027-05-01", 3.9, _NAN),  # fy2027 q1 — dropped (5th future)
        ]
    )
    return FakeTicker(earnings_dates=dates, revenue=_revenue_estimate())


def test_keeps_four_reported_quarters_newest_first():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.past] == [
        (2025, 4),
        (2025, 3),
        (2025, 2),
        (2025, 1),
    ]
    # the oldest reported quarter (fy2024 q4) fell outside the 4-quarter window
    assert all(q.fiscal_year != 2024 for q in tl.quarters)


def test_keeps_four_upcoming_quarters_soonest_first():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.future] == [
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
    ]
    # the 5th future quarter (fy2027 q1) was dropped
    assert all(q.fiscal_year != 2027 for q in tl.quarters)
    assert all(q.eps_actual is None for q in tl.future)


def test_computes_the_surprise_from_actual_and_estimate():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    q4 = tl.past[0]  # fy2025 q4: estimate 3.0, actual 3.3
    assert q4.eps_actual == 3.3 and q4.eps_estimate == 3.0
    assert q4.eps_surprise == 0.3
    assert q4.eps_surprise_percent == 10.0
    assert q4.beat is True

    q2 = next(q for q in tl.past if q.fiscal_quarter == 2)  # a miss: 2.5 vs 2.6
    assert q2.eps_surprise == -0.1
    assert q2.eps_surprise_percent == -3.85
    assert q2.beat is False


def test_forward_revenue_only_on_the_nearest_upcoming_quarters():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    future = tl.future
    assert future[0].revenue_estimate == 100e9  # 0q
    assert future[1].revenue_estimate == 110e9  # +1q
    assert future[2].revenue_estimate is None
    assert future[3].revenue_estimate is None


def test_derives_period_end_and_fiscal_labels_from_the_announcement():
    tl = provider_with(_full_ticker()).get_quarterly_earnings("AAPL")
    q1 = next(q for q in tl.past if (q.fiscal_year, q.fiscal_quarter) == (2025, 1))
    assert q1.report_date == date(2025, 5, 1)
    assert q1.period_end == date(2025, 3, 31)  # most recent quarter-end before the report


def test_reported_without_an_estimate_has_no_surprise():
    dates = _earnings_dates([("2025-05-01", _NAN, 2.5)])  # reported, estimate missing
    tl = provider_with(
        FakeTicker(earnings_dates=dates, revenue=_revenue_estimate())
    ).get_quarterly_earnings("AAPL")
    q = tl.past[0]
    assert q.eps_actual == 2.5 and q.eps_estimate is None
    assert q.eps_surprise is None and q.eps_surprise_percent is None
    assert q.beat is None


def test_empty_frame_yields_empty_timeline():
    ticker = FakeTicker(earnings_dates=_earnings_dates([]), revenue=_revenue_estimate())
    assert provider_with(ticker).get_quarterly_earnings("ZZZZ").is_empty


def test_none_frames_yield_empty_timeline():
    assert provider_with(FakeTicker()).get_quarterly_earnings("ZZZZ").is_empty


def test_vendor_error_raises_unavailable():
    ticker = FakeTicker(error=RuntimeError("yahoo down"))
    with pytest.raises(StockDataUnavailable):
        provider_with(ticker).get_quarterly_earnings("AAPL")
