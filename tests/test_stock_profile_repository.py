"""Tests for the database-backed CompanyProfileRepository.

Offline: an in-memory SQLite database stands in for the real tables. Verifies the
round-trip (CompanyProfile entity -> name on the shared stocks anchor + description on
its own table -> entity), plus the upsert semantics (one row per stock, name
fill-but-don't-clobber).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.entities import CompanyProfile
from app.stocks.ports import CachedProfile
from app.stocks.stock_profile_repository import (
    SqlCompanyProfileRepository,
    StockCompanyProfileRecord,
)
from app.stocks.stock_record import StockRecord

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlCompanyProfileRepository:
    return SqlCompanyProfileRepository(session, now=lambda: _NOW)


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_name_and_description_and_stamps_the_fetch_time(session):
    r = repo(session)
    r.upsert("AAPL", CompanyProfile(name="Apple Inc.", description="Makes phones."))

    cached = r.get("AAPL")
    assert isinstance(cached, CachedProfile)
    assert cached.profile.name == "Apple Inc."
    assert cached.profile.description == "Makes phones."
    # SQLite hands the timestamp back naive (Postgres keeps the zone); normalize.
    assert cached.fetched_at.replace(tzinfo=timezone.utc) == _NOW


def test_name_lands_on_the_shared_stocks_anchor(session):
    r = repo(session)
    r.upsert("AAPL", CompanyProfile(name="Apple Inc.", description="Makes phones."))
    # The name is on stocks, the description on the profile table.
    assert session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one() == "Apple Inc."
    assert session.execute(
        select(StockCompanyProfileRecord.description)
    ).scalar_one() == "Makes phones."


def test_upsert_replaces_in_place_without_duplicating_rows(session):
    r = repo(session)
    r.upsert("AAPL", CompanyProfile(name="Apple Inc.", description="Old."))
    r.upsert("AAPL", CompanyProfile(name="Apple Inc.", description="New."))

    assert r.get("AAPL").profile.description == "New."
    stocks = session.execute(select(func.count()).select_from(StockRecord)).scalar_one()
    rows = session.execute(
        select(func.count()).select_from(StockCompanyProfileRecord)
    ).scalar_one()
    assert stocks == 1 and rows == 1


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    # First store carries only a description (Finnhub name missed).
    r.upsert("AAPL", CompanyProfile(name=None, description="Makes phones."))
    assert session.execute(
        select(StockRecord.name).where(StockRecord.symbol == "AAPL")
    ).scalar_one() is None

    r.upsert("AAPL", CompanyProfile(name="Apple Inc.", description="Makes phones."))
    assert r.get("AAPL").profile.name == "Apple Inc."

    # A later nameless refresh must not erase the known name.
    r.upsert("AAPL", CompanyProfile(name=None, description="Still makes phones."))
    cached = r.get("AAPL")
    assert cached.profile.name == "Apple Inc."
    assert cached.profile.description == "Still makes phones."
