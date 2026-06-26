"""Tests for the database-backed ConstituentRepository.

Offline: an in-memory SQLite database stands in for the real table. Verifies the
ORM row -> Constituent entity mapping (membership flags -> the indices set), plus
the pure merge the sync script uses to fold FMP's two index feeds together.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.constituents import ConstituentRecord, SqlConstituentRepository
from app.stocks.entities import Constituent, StockIndex
from scripts.sync_constituents import build_universe


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


# --------------------------- sync merge (FMP rows -> records) ---------------------------

def test_build_universe_merges_membership_and_normalizes_sector():
    universe = build_universe(
        {
            "sp500": [
                {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"},
                {"symbol": "XOM", "name": "Exxon", "sector": "Energy"},
            ],
            "nasdaq100": [
                {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"},
                {"symbol": "ARM", "name": "Arm Holdings", "sector": "Technology"},
            ],
        }
    )
    # AAPL is in both indices; FMP "Technology" -> GICS "Information Technology".
    assert universe["AAPL"]["in_sp500"] and universe["AAPL"]["in_nasdaq100"]
    assert universe["AAPL"]["sector"] == "Information Technology"
    assert universe["XOM"]["in_sp500"] and not universe["XOM"]["in_nasdaq100"]
    assert universe["ARM"]["in_nasdaq100"] and not universe["ARM"]["in_sp500"]
    assert universe["XOM"]["sector"] == "Energy"  # already GICS, unchanged


def test_build_universe_skips_rows_without_a_symbol():
    universe = build_universe({"sp500": [{"name": "No Symbol"}, {"symbol": "  "}]})
    assert universe == {}
