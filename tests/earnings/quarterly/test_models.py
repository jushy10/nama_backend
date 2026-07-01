"""Tests for the quarterly-earnings query methods in models.py.

Offline, against in-memory SQLite: exercises the thin data-access layer directly — the
per-symbol fetch and its ordering, the whole-window delete, and the stalest-first
grouping/ordering/exclusion of the refresh query — without the repository's entity mapping.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.earnings.quarterly import models
from app.stocks.earnings.quarterly.models import StockQuarterlyEarningsRecord

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _store_quarter(session, stock_id, fy, fq, fetched_at) -> None:
    session.add(
        StockQuarterlyEarningsRecord(
            stock_id=stock_id, fiscal_year=fy, fiscal_quarter=fq, fetched_at=fetched_at
        )
    )


def test_quarters_by_symbol_orders_oldest_period_first(session):
    stock = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    _store_quarter(session, stock.id, 2026, 1, _NOW)
    _store_quarter(session, stock.id, 2025, 3, _NOW)
    _store_quarter(session, stock.id, 2025, 4, _NOW)
    session.commit()

    rows = models.quarters_by_symbol(session, "AAPL")
    assert [(r.fiscal_year, r.fiscal_quarter) for r in rows] == [
        (2025, 3),
        (2025, 4),
        (2026, 1),
    ]


def test_quarters_by_symbol_misses_cleanly(session):
    assert models.quarters_by_symbol(session, "NONE") == []


def test_delete_quarters_for_stock_only_touches_that_stock(session):
    apple = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    msft = models.get_or_create_stock(session, "MSFT", "Microsoft")
    _store_quarter(session, apple.id, 2025, 4, _NOW)
    _store_quarter(session, msft.id, 2025, 4, _NOW)
    session.commit()

    models.delete_quarters_for_stock(session, apple.id)
    session.commit()

    assert models.quarters_by_symbol(session, "AAPL") == []
    assert len(models.quarters_by_symbol(session, "MSFT")) == 1


def test_stalest_symbols_orders_oldest_first_and_limits(session):
    # Each stock's rows share a fetch stamp; the query groups by stock and orders by the
    # oldest stamp among its rows.
    aapl = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    msft = models.get_or_create_stock(session, "MSFT", "Microsoft")
    goog = models.get_or_create_stock(session, "GOOG", "Alphabet")
    _store_quarter(session, aapl.id, 2025, 4, _NOW - timedelta(days=1))  # newest
    _store_quarter(session, aapl.id, 2026, 1, _NOW - timedelta(days=1))
    _store_quarter(session, msft.id, 2025, 4, _NOW - timedelta(days=30))  # oldest
    _store_quarter(session, goog.id, 2025, 4, _NOW - timedelta(days=10))  # middle
    session.commit()

    ordered = models.stalest_symbols(session, limit=10)
    assert [s for s, _ in ordered] == ["MSFT", "GOOG", "AAPL"]
    assert models.stalest_symbols(session, limit=1) == [("MSFT", "Microsoft")]


def test_stalest_symbols_excludes_stocks_without_quarter_rows(session):
    aapl = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    _store_quarter(session, aapl.id, 2025, 4, _NOW)
    models.get_or_create_stock(session, "MSFT", "Microsoft")  # anchor only, no quarters
    session.commit()

    assert [s for s, _ in models.stalest_symbols(session, limit=10)] == ["AAPL"]


def test_stalest_symbols_returns_one_entry_per_stock(session):
    aapl = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    for fq in (1, 2, 3, 4):
        _store_quarter(session, aapl.id, 2025, fq, _NOW)
    session.commit()

    # Four rows, but the stock appears once (grouped).
    assert models.stalest_symbols(session, limit=10) == [("AAPL", "Apple Inc.")]
    total = session.execute(
        select(func.count()).select_from(StockQuarterlyEarningsRecord)
    ).scalar_one()
    assert total == 4
