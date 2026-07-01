"""Tests for the analyst-estimates query methods in models.py.

Offline, against in-memory SQLite: exercises the thin data-access layer directly — the
two row lookups and the stalest-first ordering/limit of the refresh query — without
going through the repository's entity mapping. (The shared ``stocks`` anchor's
get-or-create is tested in ``tests/stocks/test_models.py``.)
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.estimates import models
from app.stocks.estimates.models import StockAnalystEstimatesRecord

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _store(session, symbol, name, fetched_at) -> None:
    """Insert a stocks row + an estimates row for it, stamped at ``fetched_at``."""
    stock = models.get_or_create_stock(session, symbol, name)
    session.add(StockAnalystEstimatesRecord(stock_id=stock.id, fetched_at=fetched_at))
    session.commit()


def test_estimates_by_symbol_and_stock_id(session):
    _store(session, "AAPL", "Apple Inc.", _NOW)
    by_symbol = models.estimates_by_symbol(session, "AAPL")
    assert by_symbol is not None
    assert models.estimates_by_stock_id(session, by_symbol.stock_id).id == by_symbol.id


def test_lookups_miss_cleanly(session):
    assert models.estimates_by_symbol(session, "NONE") is None
    # a stock with an anchor but no estimates row is also a miss
    stock = models.get_or_create_stock(session, "MSFT", None)
    session.commit()
    assert models.estimates_by_stock_id(session, stock.id) is None


def test_stalest_symbols_orders_oldest_first_and_limits(session):
    _store(session, "AAPL", "Apple Inc.", _NOW - timedelta(days=1))   # newest
    _store(session, "MSFT", "Microsoft", _NOW - timedelta(days=30))   # oldest
    _store(session, "GOOG", "Alphabet", _NOW - timedelta(days=10))    # middle

    ordered = models.stalest_symbols(session, limit=10)
    assert [s for s, _ in ordered] == ["MSFT", "GOOG", "AAPL"]
    assert models.stalest_symbols(session, limit=1) == [("MSFT", "Microsoft")]


def test_stalest_symbols_excludes_symbols_without_an_estimates_row(session):
    _store(session, "AAPL", "Apple Inc.", _NOW)
    models.get_or_create_stock(session, "MSFT", "Microsoft")  # anchor only, no estimates
    session.commit()
    assert [s for s, _ in models.stalest_symbols(session, limit=10)] == ["AAPL"]
