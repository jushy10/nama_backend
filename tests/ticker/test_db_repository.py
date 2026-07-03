"""Tests for the SQL-backed TickerRepository.

Offline, against in-memory SQLite: exercises the exchange read/fill round-trip over
the shared ``stocks`` anchor — miss reads as None, a save creates the row and is
committed, and a stored value is never clobbered — independent of HTTP and vendors.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.ticker.db_repository import SqlTickerRepository


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def test_get_exchange_miss_is_none(session):
    assert SqlTickerRepository(session).get_exchange("MU") is None


def test_save_then_get_round_trips_and_creates_the_anchor_row(session):
    repo = SqlTickerRepository(session)
    repo.save_exchange("MU", "NASDAQ")
    assert repo.get_exchange("MU") == "NASDAQ"


def test_save_never_clobbers_a_stored_exchange(session):
    repo = SqlTickerRepository(session)
    repo.save_exchange("MU", "NASDAQ")
    repo.save_exchange("MU", "NYSE")  # a later differing value is ignored
    assert repo.get_exchange("MU") == "NASDAQ"


def test_save_commits_its_own_write(session):
    # A successful lazy fill must be durable independent of the request: the
    # value survives a rollback of the surrounding session.
    repo = SqlTickerRepository(session)
    repo.save_exchange("MU", "NASDAQ")
    session.rollback()
    assert repo.get_exchange("MU") == "NASDAQ"
