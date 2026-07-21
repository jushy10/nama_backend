import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.catalog.anchor.models import StockRecord
from app.stocks.company.ticker.db_repository import SqlTickerRepository
from app.stocks.company.ticker.repository import StoredTickerFacts


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


def test_get_facts_serves_the_screen_and_growth_facts_off_the_anchor(session):
    # The universe/annual syncs write these onto the shared row; the card only
    # reads them — never fills them — so a plain anchor read must return them all.
    session.add(
        StockRecord(
            ticker="MU",
            name="Micron Technology",
            exchange="NASDAQ",
            market_cap=1.09e12,
            sector="technology",
            industry="semiconductors",
            revenue_growth_yoy=61.6,
            eps_growth_yoy=587.4,
        )
    )
    session.commit()
    assert SqlTickerRepository(session).get_facts("MU") == StoredTickerFacts(
        name="Micron Technology",
        exchange="NASDAQ",
        market_cap=1.09e12,
        sector="technology",
        industry="semiconductors",
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
    )
