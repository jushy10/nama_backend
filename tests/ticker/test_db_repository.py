"""Tests for the SQL-backed TickerRepository.

Offline, against in-memory SQLite: exercises the name/exchange read/fill round-trips
over the shared ``stocks`` anchor — a miss reads as per-field None, a save creates the
row and is committed, and a stored value is never clobbered — independent of HTTP and
vendors.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.ticker.db_repository import SqlTickerRepository
from app.stocks.ticker.repository import StoredTickerFacts


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def test_get_facts_miss_is_all_none(session):
    assert SqlTickerRepository(session).get_facts("MU") == StoredTickerFacts(None, None)


def test_saves_round_trip_and_create_the_anchor_row(session):
    repo = SqlTickerRepository(session)
    repo.save_name("MU", "Micron Technology")
    repo.save_exchange("MU", "NASDAQ")
    assert repo.get_facts("MU") == StoredTickerFacts("Micron Technology", "NASDAQ")


def test_each_fact_fills_independently(session):
    repo = SqlTickerRepository(session)
    repo.save_exchange("MU", "NASDAQ")  # row exists, name still unknown
    assert repo.get_facts("MU") == StoredTickerFacts(None, "NASDAQ")


def test_saves_never_clobber_stored_values(session):
    repo = SqlTickerRepository(session)
    repo.save_name("MU", "Micron Technology")
    repo.save_exchange("MU", "NASDAQ")
    repo.save_name("MU", "Micron Corp.")  # later differing values are ignored
    repo.save_exchange("MU", "NYSE")
    assert repo.get_facts("MU") == StoredTickerFacts("Micron Technology", "NASDAQ")


def test_saves_commit_their_own_write(session):
    # A successful lazy fill must be durable independent of the request: the
    # values survive a rollback of the surrounding session.
    repo = SqlTickerRepository(session)
    repo.save_name("MU", "Micron Technology")
    repo.save_exchange("MU", "NASDAQ")
    session.rollback()
    assert repo.get_facts("MU") == StoredTickerFacts("Micron Technology", "NASDAQ")
