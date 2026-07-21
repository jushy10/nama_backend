from datetime import date

import pytest

from app.stocks.adapters.annual_earnings_estimates_adapter import (
    AnnualEarningsEstimatesProvider,
)
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.repository import AnnualEarningsRepository
from app.stocks.exceptions import StockDataUnavailable


def _reported(year: int, eps: float) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=year,
        period_end=date(year, 9, 30),
        eps_actual=eps,
        eps_estimate=None,
        revenue_actual=380e9,
        revenue_estimate=None,
        net_income=95e9,
    )


def _upcoming(year: int, eps: float | None, revenue: float | None) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=year,
        period_end=date(year, 9, 30),
        eps_actual=None,  # not yet reported — this is what marks it upcoming
        eps_estimate=eps,
        revenue_actual=None,
        revenue_estimate=revenue,
    )


class FakeRepo(AnnualEarningsRepository):
    def __init__(self, timeline: AnnualEarningsTimeline | None = None, fail=False):
        self._timeline = timeline
        self._fail = fail

    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        if self._fail:
            raise RuntimeError("db down")
        return self._timeline

    def upsert(self, symbol, name, timeline) -> None:  # pragma: no cover
        raise AssertionError("the estimates projection never writes")

    def refresh_targets(self, limit: int):  # pragma: no cover
        return []


def test_projects_first_two_upcoming_years_as_fy1_and_fy2():
    timeline = AnnualEarningsTimeline(
        symbol="AAPL",
        years=(
            _reported(2024, 6.1),
            _reported(2025, 7.3),
            _upcoming(2026, 8.0, 420e9),
            _upcoming(2027, 9.2, 455e9),
        ),
    )
    est = AnnualEarningsEstimatesProvider(FakeRepo(timeline)).get_estimates("AAPL")
    assert est.fiscal_year == 2026
    assert est.period_end == date(2026, 9, 30)
    assert est.eps_avg == 8.0
    assert est.revenue_avg == 420e9
    assert est.fiscal_year_fy2 == 2027
    assert est.eps_avg_fy2 == 9.2
    assert est.revenue_avg_fy2 == 455e9
    # And the derived forward growth works off the projected pair.
    assert est.forward_eps_growth() == 15.0


def test_single_upcoming_year_leaves_fy2_unset():
    timeline = AnnualEarningsTimeline(
        symbol="AAPL",
        years=(_reported(2025, 7.3), _upcoming(2026, 8.0, 420e9)),
    )
    est = AnnualEarningsEstimatesProvider(FakeRepo(timeline)).get_estimates("AAPL")
    assert est.fiscal_year == 2026 and est.eps_avg == 8.0
    assert est.fiscal_year_fy2 is None
    assert est.eps_avg_fy2 is None
    assert est.forward_eps_growth() is None  # only FY1, nothing to compare


def test_uncached_symbol_yields_empty_block():
    est = AnnualEarningsEstimatesProvider(FakeRepo(None)).get_estimates("ZZZZ")
    assert est.is_empty


def test_reported_only_timeline_yields_empty_block():
    # A cached symbol whose forward years Yahoo didn't estimate: history without
    # consensus, so there's no forward block to attach.
    timeline = AnnualEarningsTimeline(
        symbol="AAPL", years=(_reported(2024, 6.1), _reported(2025, 7.3))
    )
    est = AnnualEarningsEstimatesProvider(FakeRepo(timeline)).get_estimates("AAPL")
    assert est.is_empty


def test_storage_failure_becomes_domain_error():
    provider = AnnualEarningsEstimatesProvider(FakeRepo(fail=True))
    with pytest.raises(StockDataUnavailable):
        provider.get_estimates("AAPL")
