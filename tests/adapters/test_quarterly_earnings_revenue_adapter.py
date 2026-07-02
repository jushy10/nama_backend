"""Tests for the quarterly-earnings-backed RevenueHistoryProvider.

Offline: a hand-written fake quarterly provider stands in for the DB cache, so this
exercises only the projection — reported quarters with a period end and a revenue
become map entries, everything else is dropped, and inner failures pass through as
the domain exceptions the earnings use case already treats as best-effort.
"""

from datetime import date

import pytest

from app.stocks.adapters.quarterly_earnings_revenue_adapter import (
    QuarterlyEarningsRevenueProvider,
)
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.exceptions import StockDataUnavailable


def _quarter(
    year: int,
    quarter: int,
    *,
    eps_actual: float | None,
    revenue: float | None,
    period_end: date | None,
) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=year, fiscal_quarter=quarter, period_end=period_end,
        report_date=None, eps_actual=eps_actual, eps_estimate=1.9,
        eps_surprise=None, eps_surprise_percent=None, revenue_estimate=None,
        revenue_actual=revenue,
    )


class _FakeQuarterly(QuarterlyEarningsProvider):
    def __init__(self, timeline=None, error=None) -> None:
        self._timeline = timeline
        self._error = error

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        if self._error is not None:
            raise self._error
        return self._timeline


def test_maps_reported_quarters_to_revenue_by_period_end():
    timeline = QuarterlyEarningsTimeline(
        "AAPL",
        (
            _quarter(2025, 4, eps_actual=2.8, revenue=143e9, period_end=date(2025, 12, 31)),
            _quarter(2026, 1, eps_actual=2.0, revenue=111e9, period_end=date(2026, 3, 31)),
            # No revenue stored -> not in the map (best-effort overlay).
            _quarter(2026, 2, eps_actual=2.1, revenue=None, period_end=date(2026, 6, 30)),
            # Upcoming quarter -> no revenue actual by definition.
            _quarter(2026, 3, eps_actual=None, revenue=None, period_end=date(2026, 9, 30)),
        ),
    )
    revenue = QuarterlyEarningsRevenueProvider(_FakeQuarterly(timeline)).get_quarterly_revenue("AAPL")
    assert revenue == {date(2025, 12, 31): 143e9, date(2026, 3, 31): 111e9}


def test_quarter_without_period_end_is_dropped():
    # The earnings use case aligns by period end, so an unlabeled quarter can't be keyed.
    timeline = QuarterlyEarningsTimeline(
        "AAPL", (_quarter(2026, 1, eps_actual=2.0, revenue=111e9, period_end=None),)
    )
    revenue = QuarterlyEarningsRevenueProvider(_FakeQuarterly(timeline)).get_quarterly_revenue("AAPL")
    assert revenue == {}


def test_empty_timeline_yields_empty_map():
    provider = QuarterlyEarningsRevenueProvider(
        _FakeQuarterly(QuarterlyEarningsTimeline("ZZZZ", ()))
    )
    assert provider.get_quarterly_revenue("ZZZZ") == {}


def test_inner_failure_passes_through_as_domain_error():
    provider = QuarterlyEarningsRevenueProvider(
        _FakeQuarterly(error=StockDataUnavailable("AAPL", "yahoo down"))
    )
    with pytest.raises(StockDataUnavailable):
        provider.get_quarterly_revenue("AAPL")
