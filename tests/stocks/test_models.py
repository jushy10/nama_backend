"""Tests for the shared ``stocks`` anchor model + get_or_create_stock.

Offline, against in-memory SQLite: exercises the get-or-create semantics directly —
one row per symbol, name filled but never clobbered — independent of any feature slice.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.stocks import models


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def test_get_or_create_creates_then_returns_the_same_row(session):
    a = models.get_or_create_stock(session, "AAPL", "Apple Inc.")
    session.commit()
    b = models.get_or_create_stock(session, "AAPL", None)
    assert a.id == b.id  # same row, not a duplicate


def test_get_or_create_fills_missing_name_but_never_clobbers(session):
    models.get_or_create_stock(session, "AAPL", None)
    assert models.get_or_create_stock(session, "AAPL", "Apple Inc.").name == "Apple Inc."
    # a later nameless call must not erase the known name
    assert models.get_or_create_stock(session, "AAPL", None).name == "Apple Inc."


def test_exchange_missing_row_or_value_reads_as_none(session):
    assert models.exchange_by_symbol(session, "AAPL") is None  # no row at all
    models.get_or_create_stock(session, "AAPL", None)
    assert models.exchange_by_symbol(session, "AAPL") is None  # row, no exchange yet


def test_fill_exchange_creates_the_row_and_never_clobbers(session):
    models.fill_exchange(session, "AAPL", "NASDAQ")  # creates the anchor row too
    assert models.exchange_by_symbol(session, "AAPL") == "NASDAQ"
    # same no-clobber stance as the name: the first learned value settles it
    models.fill_exchange(session, "AAPL", "NYSE")
    assert models.exchange_by_symbol(session, "AAPL") == "NASDAQ"
