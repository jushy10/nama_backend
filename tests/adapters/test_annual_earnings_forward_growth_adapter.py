"""Tests for the forward-growth adapter over the annual-earnings cache.

Offline: an in-memory SQLite database seeded through the slice's own repository stands in
for the real table. Verifies the projection — one batch query, FY1 = the soonest upcoming
year against the latest reported base, growth math via the entity — plus the omissions:
never-stored symbols and symbols with no upcoming year simply don't appear in the map,
and a forward-only symbol (Yahoo-gated income statement) keeps its estimates with an
unrankable (``None``) growth.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.adapters.annual_earnings_forward_growth_adapter import (
    AnnualEarningsForwardGrowthProvider,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _reported(fy: int, eps: float, revenue: float | None) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=fy, period_end=date(fy, 12, 31), eps_actual=eps,
        eps_estimate=None, revenue_actual=revenue, revenue_estimate=None,
    )


def _upcoming(fy: int, eps: float | None, revenue: float | None) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=fy, period_end=date(fy, 12, 31), eps_actual=None,
        eps_estimate=eps, revenue_actual=None, revenue_estimate=revenue,
    )


def _store(session, symbol: str, *years: AnnualEarnings) -> None:
    SqlAnnualEarningsRepository(session).upsert(
        symbol, None, AnnualEarningsTimeline(symbol=symbol, years=tuple(years))
    )


def test_projects_latest_reported_vs_soonest_upcoming(session):
    _store(
        session,
        "AAPL",
        _reported(2023, 5.5, 380e9),
        _reported(2024, 6.0, 400e9),  # the base: latest reported, not 2023
        _upcoming(2025, 6.9, 460e9),  # FY1: soonest upcoming, not 2026
        _upcoming(2026, 8.0, 500e9),
    )

    growth = AnnualEarningsForwardGrowthProvider(session).get_forward_growth(["AAPL"])

    fg = growth["AAPL"]
    assert (fg.fiscal_year, fg.prior_fiscal_year) == (2025, 2024)
    assert (fg.eps_actual, fg.eps_estimate) == (6.0, 6.9)
    assert (fg.revenue_actual, fg.revenue_estimate) == (400e9, 460e9)
    # 6.0 -> 6.9 and 400e9 -> 460e9 are both +15%.
    assert fg.expected_eps_growth == 15.0
    assert fg.expected_revenue_growth == 15.0


def test_batch_covers_only_symbols_with_a_stored_upcoming_year(session):
    _store(session, "AAPL", _reported(2024, 6.0, 400e9), _upcoming(2025, 6.9, 460e9))
    # Reported-only: nothing forward-looking to screen on.
    _store(session, "OLD", _reported(2024, 3.0, 50e9))

    growth = AnnualEarningsForwardGrowthProvider(session).get_forward_growth(
        ["AAPL", "OLD", "NEVER"]  # NEVER was never cached at all
    )

    assert set(growth) == {"AAPL"}


def test_forward_only_symbol_yields_unrankable_growth(session):
    # Yahoo's income-statement gate can leave a symbol with estimates but no
    # reported base — the legs survive, the growth percent just can't be computed.
    _store(session, "MU", _upcoming(2026, 8.0, 40e9))

    fg = AnnualEarningsForwardGrowthProvider(session).get_forward_growth(["MU"])["MU"]

    assert (fg.fiscal_year, fg.prior_fiscal_year) == (2026, None)
    assert fg.eps_actual is None and fg.revenue_actual is None
    assert fg.expected_eps_growth is None
    assert fg.expected_revenue_growth is None


def test_growth_off_a_non_positive_base_is_none(session):
    # A loss year (or an expected loss) has no meaningful growth percent.
    _store(session, "LOSS", _reported(2024, -1.0, 400e9), _upcoming(2025, 2.0, 380e9))

    fg = AnnualEarningsForwardGrowthProvider(session).get_forward_growth(["LOSS"])["LOSS"]

    assert fg.expected_eps_growth is None  # loss -> profit: no percent
    assert fg.expected_revenue_growth == -5.0  # revenue still computes (and can shrink)


def test_empty_symbol_list_is_an_empty_map(session):
    assert AnnualEarningsForwardGrowthProvider(session).get_forward_growth([]) == {}
