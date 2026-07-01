"""Tests for the database-backed AnalystEstimatesRepository.

Offline: an in-memory SQLite database stands in for the real tables. Verifies the
round-trip (AnalystEstimates entity -> rows -> entity), including that the FY2 figures
are preserved so the reconstructed entity's forward growth still computes, plus the
upsert semantics (one row per stock, name fill-but-don't-clobber).
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.entities import AnalystEstimates, ForwardEstimate
from app.stocks.estimates.estimates_ports import CachedEstimates
from app.stocks.estimates.stock_estimates_repository import (
    SqlAnalystEstimatesRepository,
    StockAnalystEstimatesRecord,
    StockRecord,
)

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlAnalystEstimatesRepository:
    return SqlAnalystEstimatesRepository(session, now=lambda: _NOW)


def an_estimates(**overrides) -> AnalystEstimates:
    base = dict(
        fiscal_year=2026,
        period_end=date(2026, 9, 30),
        eps_avg=8.0,
        eps_low=7.4,
        eps_high=8.6,
        revenue_avg=420_000_000_000.0,
        num_analysts_eps=30,
        num_analysts_revenue=28,
        eps_avg_fy2=9.2,
        fiscal_year_fy2=2027,
        forward_years=(
            ForwardEstimate(2026, date(2026, 9, 30), 8.0, 420_000_000_000.0),
            ForwardEstimate(2027, date(2027, 9, 30), 9.2, 455_000_000_000.0),
        ),
    )
    base.update(overrides)
    return AnalystEstimates(**base)


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_the_estimate_and_stamps_the_fetch_time(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", an_estimates())

    cached = r.get("AAPL")
    assert isinstance(cached, CachedEstimates)
    # SQLite hands the timestamp back naive (Postgres keeps the zone); normalize to
    # UTC the way the cache decorator does before comparing.
    assert cached.fetched_at.replace(tzinfo=timezone.utc) == _NOW
    est = cached.estimates
    assert est.fiscal_year == 2026
    assert est.eps_avg == 8.0
    assert est.eps_low == 7.4 and est.eps_high == 8.6
    assert est.revenue_avg == 420_000_000_000.0
    assert est.num_analysts_eps == 30 and est.num_analysts_revenue == 28
    assert est.eps_avg_fy2 == 9.2 and est.fiscal_year_fy2 == 2027


def test_preserves_fy2_revenue_so_forward_growth_still_computes(session):
    # FY2 revenue isn't a headline field — it lives only in the series — so the
    # round-trip has to carry it for forward_revenue_growth to survive.
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", an_estimates())
    est = r.get("AAPL").estimates

    assert est.forward_years[1].revenue_avg == 455_000_000_000.0
    assert est.forward_eps_growth() == 15.0  # 9.2/8.0 - 1
    assert est.forward_revenue_growth() == 8.33  # 455/420 - 1
    assert est.forward_pe(160.0) == 20.0  # 160 / 8.0


def test_creates_the_parent_stock_row(session):
    repo(session).upsert("AAPL", "Apple Inc.", an_estimates())
    stock = session.execute(
        select(StockRecord).where(StockRecord.symbol == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc."
    assert stock.id is not None


def test_upsert_replaces_in_place_without_duplicating_rows(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", an_estimates())
    r.upsert("AAPL", "Apple Inc.", an_estimates(eps_avg=8.5))

    assert r.get("AAPL").estimates.eps_avg == 8.5
    stocks = session.execute(select(func.count()).select_from(StockRecord)).scalar_one()
    rows = session.execute(
        select(func.count()).select_from(StockAnalystEstimatesRecord)
    ).scalar_one()
    assert stocks == 1 and rows == 1


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    r.upsert("AAPL", None, an_estimates())  # stored with no name yet
    assert session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one() is None

    r.upsert("AAPL", "Apple Inc.", an_estimates())  # later fills it in
    name = session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one()
    assert name == "Apple Inc."

    r.upsert("AAPL", None, an_estimates())  # a nameless refresh must not erase it
    name = session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one()
    assert name == "Apple Inc."


def test_stores_an_empty_estimate_so_uncovered_symbols_arent_refetched(session):
    r = repo(session)
    empty = AnalystEstimates(
        fiscal_year=None, period_end=None, eps_avg=None, eps_low=None, eps_high=None,
        revenue_avg=None, num_analysts_eps=None, num_analysts_revenue=None,
    )
    r.upsert("ZZZZ", None, empty)
    cached = r.get("ZZZZ")
    assert cached is not None
    assert cached.estimates.is_empty
