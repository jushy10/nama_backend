"""Tests for the database-backed QuarterlyEarningsRepository.

Offline: an in-memory SQLite database stands in for the real table. Verifies the
round-trip (entities -> rows -> entities) including the canonical timeline order,
whole-window replace on upsert (no duplicate rows, other stocks untouched), the parent
``stocks`` row + name fill-but-don't-clobber, and a clean miss.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.earnings.quarterly.db_repository import SqlQuarterlyEarningsRepository
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.models import (
    StockQuarterlyEarningsRecord,
    StockRecord,
)

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlQuarterlyEarningsRepository:
    return SqlQuarterlyEarningsRepository(session, now=lambda: _NOW)


def _reported(
    fy: int, fq: int, actual: float, estimate: float, revenue_actual: float | None = None
) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=date(fy, fq * 3, 28),
        report_date=date(fy, fq * 3, 28),
        eps_actual=actual,
        eps_estimate=estimate,
        eps_surprise=round(actual - estimate, 4),
        eps_surprise_percent=round((actual - estimate) / abs(estimate) * 100, 2),
        revenue_estimate=None,
        revenue_actual=revenue_actual,
    )


def _upcoming(fy: int, fq: int, estimate: float, revenue: float | None) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=date(fy, fq * 3, 28),
        report_date=date(fy, fq * 3, 28),
        eps_actual=None,
        eps_estimate=estimate,
        eps_surprise=None,
        eps_surprise_percent=None,
        revenue_estimate=revenue,
    )


def _timeline() -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(
            _reported(2025, 4, 3.0, 2.8, revenue_actual=5.0e9),
            _reported(2025, 3, 2.5, 2.4),
            _upcoming(2026, 1, 3.1, 100e9),
            _upcoming(2026, 2, 3.3, 110e9),
        ),
    )


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_the_timeline(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())

    tl = r.get("AAPL")
    assert isinstance(tl, QuarterlyEarningsTimeline)
    # Canonical order: reported newest-first, then upcoming soonest-first — regardless
    # of the insert order.
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.quarters] == [
        (2025, 4),
        (2025, 3),
        (2026, 1),
        (2026, 2),
    ]
    q4 = tl.quarters[0]
    assert q4.eps_actual == 3.0 and q4.eps_estimate == 2.8
    assert q4.eps_surprise == 0.2 and q4.eps_surprise_percent == 7.14
    assert q4.revenue_actual == 5.0e9
    assert q4.is_reported and q4.beat is True

    upcoming = tl.future[0]
    assert upcoming.eps_actual is None and upcoming.revenue_estimate == 100e9
    assert upcoming.revenue_actual is None
    assert upcoming.is_reported is False


def test_upsert_stamps_the_fetch_time(session):
    # fetched_at isn't part of the read shape any more, but it's still written — the cron's
    # stalest-first refresh orders by it — so verify the stamp lands on the rows. SQLite
    # hands the timestamp back naive (Postgres keeps the zone); normalize to UTC.
    repo(session).upsert("AAPL", "Apple Inc.", _timeline())
    stamp = (
        session.execute(select(StockQuarterlyEarningsRecord.fetched_at)).scalars().first()
    )
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_upsert_replaces_the_whole_window(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())  # 4 quarters
    r.upsert(
        "AAPL",
        "Apple Inc.",
        QuarterlyEarningsTimeline("AAPL", (_reported(2026, 1, 4.0, 3.9),)),
    )  # now just 1

    tl = r.get("AAPL")
    assert [(q.fiscal_year, q.fiscal_quarter) for q in tl.quarters] == [(2026, 1)]
    rows = session.execute(
        select(func.count()).select_from(StockQuarterlyEarningsRecord)
    ).scalar_one()
    assert rows == 1  # old window cleared, not duplicated


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())
    r.upsert("MSFT", "Microsoft", QuarterlyEarningsTimeline("MSFT", (_reported(2025, 4, 2.9, 2.7),)))

    r.upsert("AAPL", "Apple Inc.", QuarterlyEarningsTimeline("AAPL", (_reported(2026, 1, 4.0, 3.9),)))

    assert len(r.get("MSFT").quarters) == 1  # MSFT survived AAPL's rewrite


def test_creates_the_parent_stock_row(session):
    repo(session).upsert("AAPL", "Apple Inc.", _timeline())
    stock = session.execute(
        select(StockRecord).where(StockRecord.symbol == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc." and stock.id is not None


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    r.upsert("AAPL", None, _timeline())
    assert session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one() is None

    r.upsert("AAPL", "Apple Inc.", _timeline())
    r.upsert("AAPL", None, _timeline())  # a nameless refresh must not erase it
    assert session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one() == "Apple Inc."


def test_refresh_targets_orders_stalest_first_and_carries_the_name(session):
    # refresh_targets wraps the stalest-first query the cron walks; a stock's rows share a
    # fetch stamp, so an older upsert sorts ahead of a newer one, each paired with its name.
    older = SqlQuarterlyEarningsRepository(session, now=lambda: _NOW - timedelta(days=10))
    newer = SqlQuarterlyEarningsRepository(session, now=lambda: _NOW)
    older.upsert(
        "MSFT",
        "Microsoft",
        QuarterlyEarningsTimeline("MSFT", (_reported(2025, 4, 2.9, 2.7),)),
    )
    newer.upsert("AAPL", "Apple Inc.", _timeline())

    targets = newer.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "AAPL"]  # stalest first
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert newer.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected
