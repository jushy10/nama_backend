"""Tests for the database-backed ConstituentRepository.

Offline: an in-memory SQLite database stands in for the real table. Verifies the
ORM row -> Constituent entity mapping (membership flags -> the indices set).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.constituents import ConstituentRecord, SqlConstituentRepository
from app.stocks.entities import Constituent, StockIndex


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _add(session, **fields):
    session.add(ConstituentRecord(**fields))
    session.commit()


# --------------------------- repository (row -> entity) ---------------------------

def test_maps_row_to_entity(session):
    _add(
        session,
        symbol="AAPL",
        name="Apple Inc.",
        sector="Information Technology",
        in_sp500=True,
        in_nasdaq100=True,
    )
    (apple,) = SqlConstituentRepository(session).all()
    assert isinstance(apple, Constituent)
    assert apple.symbol == "AAPL"
    assert apple.name == "Apple Inc."
    assert apple.sector == "Information Technology"
    assert apple.in_index(StockIndex.SP500)
    assert apple.in_index(StockIndex.NASDAQ100)


def test_membership_flags_become_indices(session):
    _add(session, symbol="XOM", sector="Energy", in_sp500=True, in_nasdaq100=False)
    _add(session, symbol="ARM", sector="Information Technology", in_sp500=False, in_nasdaq100=True)
    by_symbol = {c.symbol: c for c in SqlConstituentRepository(session).all()}
    assert by_symbol["XOM"].indices == frozenset({"sp500"})
    assert by_symbol["ARM"].indices == frozenset({"nasdaq100"})
    assert not by_symbol["XOM"].in_index(StockIndex.NASDAQ100)


def test_nullable_name_and_sector(session):
    _add(session, symbol="ZZZZ", in_sp500=True)
    (z,) = SqlConstituentRepository(session).all()
    assert z.name is None and z.sector is None
    assert z.indices == frozenset({"sp500"})


def test_empty_table_returns_empty_tuple(session):
    assert SqlConstituentRepository(session).all() == ()
