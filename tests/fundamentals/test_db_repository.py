"""Tests for the database-backed FundamentalsRepository.

Offline: an in-memory SQLite database stands in for the real ``stocks`` table (fundamentals have
no table of their own). Verifies ``upsert`` lands every figure on the anchor and stamps the sync
time (creating an absent anchor, never clobbering a known name), overwrites a stale figure to
``None``, and that ``refresh_targets`` orders the work-list un-synced-first then stalest, honours
the cap, and carries each row's name.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.fundamentals.db_repository import SqlFundamentalsRepository
from app.stocks.fundamentals.entities import Fundamentals
from app.stocks.fundamentals.repository import RefreshTarget
from app.stocks.stocks.models import StockRecord, get_or_create_stock


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _row(session, ticker) -> StockRecord:
    return session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one()


def _at(*, day: int):
    return lambda: datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc)


def _synced(row) -> datetime:
    """The row's sync stamp as tz-aware UTC. SQLite (unlike Postgres) drops the tzinfo on a
    DateTime(timezone=True) round-trip, returning a naive UTC value — re-attach it so the
    comparison is stable across both backends."""
    stamp = row.fundamentals_synced_at
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _full() -> Fundamentals:
    return Fundamentals(
        gross_margin=44.0,
        operating_margin=30.0,
        net_margin=25.0,
        return_on_equity=147.4,
        current_ratio=0.9,
        debt_to_equity=1.5,
        beta=1.2,
        book_value_per_share=4.2,
        sales_per_share=25.0,
        dividend_per_share=1.0,
        ebitda=130_000_000_000.0,
        total_debt=100_000_000_000.0,
        cash_and_equivalents=60_000_000_000.0,
        shares_outstanding=16_000_000_000.0,
    )


def test_upsert_lands_every_figure_and_stamps_the_sync_time(session):
    SqlFundamentalsRepository(session, now=_at(day=4)).upsert("AAPL", "Apple Inc.", _full())

    row = _row(session, "AAPL")
    assert row.name == "Apple Inc."  # created the anchor with its name
    assert (row.gross_margin, row.operating_margin, row.net_margin) == (44.0, 30.0, 25.0)
    assert (row.return_on_equity, row.current_ratio, row.debt_to_equity) == (147.4, 0.9, 1.5)
    assert row.beta == 1.2
    assert (row.book_value_per_share, row.sales_per_share, row.dividend_per_share) == (4.2, 25.0, 1.0)
    # The enterprise-value inputs land too (the materialized ev_to_ebitda snapshot is the
    # universe pass's job, so it stays null here).
    assert (row.ebitda, row.total_debt, row.cash_and_equivalents) == (
        130_000_000_000.0, 100_000_000_000.0, 60_000_000_000.0,
    )
    assert row.shares_outstanding == 16_000_000_000.0
    assert row.ev_to_ebitda is None
    assert _synced(row) == datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def test_upsert_overwrites_a_stale_figure_to_none(session):
    repo = SqlFundamentalsRepository(session, now=_at(day=4))
    repo.upsert("AAPL", None, Fundamentals(net_margin=25.0, dividend_per_share=1.0))
    # A later refresh where Yahoo no longer carries the dividend clears it (a moving snapshot,
    # not fill-once), and re-stamps.
    repo2 = SqlFundamentalsRepository(session, now=_at(day=11))
    repo2.upsert("AAPL", None, Fundamentals(net_margin=26.0, dividend_per_share=None))

    row = _row(session, "AAPL")
    assert row.net_margin == 26.0
    assert row.dividend_per_share is None
    assert _synced(row) == datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def test_upsert_never_clobbers_a_known_name_with_none(session):
    get_or_create_stock(session, "AAPL", "Apple Inc.")
    session.commit()
    SqlFundamentalsRepository(session).upsert("AAPL", None, Fundamentals(net_margin=25.0))
    assert _row(session, "AAPL").name == "Apple Inc."  # the settled name survives a nameless write


def test_refresh_targets_orders_unsynced_first_then_stalest(session):
    # OLD synced longest ago, NEW synced most recently, FRESH never synced.
    SqlFundamentalsRepository(session, now=_at(day=2)).upsert("OLD", None, Fundamentals(beta=1.0))
    SqlFundamentalsRepository(session, now=_at(day=9)).upsert("NEW", None, Fundamentals(beta=1.0))
    get_or_create_stock(session, "FRESH", "Fresh Co.")  # never synced -> NULL stamp
    session.commit()

    targets = SqlFundamentalsRepository(session).refresh_targets(None)

    assert [t.symbol for t in targets] == ["FRESH", "OLD", "NEW"]  # NULL first, then oldest→newest
    assert targets[0] == RefreshTarget("FRESH", "Fresh Co.")  # carries the anchor name


def test_refresh_targets_honours_the_cap(session):
    get_or_create_stock(session, "A", None)
    get_or_create_stock(session, "B", None)
    get_or_create_stock(session, "C", None)
    session.commit()

    targets = SqlFundamentalsRepository(session).refresh_targets(2)

    assert len(targets) == 2  # only the two neediest this run
