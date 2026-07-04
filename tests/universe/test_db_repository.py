"""Tests for the database-backed UniverseRepository.

Offline: an in-memory SQLite database stands in for the real tables. Verifies the reconcile
(insert new / update in place / remove absent — with the ``stocks`` anchor rows preserved),
the anchor name+exchange fill-but-don't-clobber, the screen stamp, and search (ilike on
ticker or name, largest market cap first, limit respected).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.stocks.models import StockRecord
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.models import StockUniverseRecord

_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=_NOW) -> SqlUniverseRepository:
    return SqlUniverseRepository(session, now=lambda: now)


def _stock(ticker, *, name=None, exchange=None, market_cap=1e10, sector=None):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
    )


def _anchor(session, ticker) -> tuple[str | None, str | None]:
    row = session.execute(
        select(StockRecord.name, StockRecord.exchange).where(
            StockRecord.ticker == ticker
        )
    ).one()
    return (row.name, row.exchange)


def _universe_count(session) -> int:
    return session.execute(
        select(func.count()).select_from(StockUniverseRecord)
    ).scalar_one()


def test_replace_inserts_new_members_fills_the_anchor_and_stamps(session):
    counts = repo(session).replace_universe(
        (
            _stock(
                "AAPL",
                name="Apple Inc.",
                exchange="NASDAQ",
                market_cap=3e12,
                sector="Technology",
            ),
            _stock("XOM", name="Exxon Mobil", market_cap=5e11, sector="Energy"),
        )
    )

    assert (counts.added, counts.updated, counts.removed) == (2, 0, 0)
    assert _universe_count(session) == 2
    assert _anchor(session, "AAPL") == ("Apple Inc.", "NASDAQ")
    assert _anchor(session, "XOM") == ("Exxon Mobil", None)  # no exchange on the screen
    # The screen time is stamped on each membership row (SQLite hands it back naive).
    stamp = session.execute(
        select(StockUniverseRecord.screened_at)
    ).scalars().first()
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_replace_updates_market_cap_in_place(session):
    r = repo(session)
    r.replace_universe((_stock("AAPL", market_cap=3.0e12, sector="Technology"),))
    counts = r.replace_universe((_stock("AAPL", market_cap=3.4e12, sector="Tech"),))

    assert (counts.added, counts.updated, counts.removed) == (0, 1, 0)
    assert _universe_count(session) == 1  # updated, not duplicated
    row = session.execute(select(StockUniverseRecord)).scalars().one()
    assert row.market_cap == 3.4e12 and row.sector == "Tech"


def test_replace_removes_absent_members_but_keeps_their_anchor(session):
    r = repo(session)
    r.replace_universe(
        (_stock("AAPL", name="Apple Inc."), _stock("XOM", name="Exxon Mobil"))
    )
    # A later screen no longer lists XOM (fell below the floor / delisted).
    counts = r.replace_universe((_stock("AAPL", name="Apple Inc."),))

    assert (counts.added, counts.updated, counts.removed) == (0, 1, 1)
    assert _universe_count(session) == 1  # only AAPL remains a member
    # XOM's membership row is gone, but its anchor row survives (other slices may use it).
    assert _anchor(session, "XOM") == ("Exxon Mobil", None)


def test_replace_fills_missing_anchor_facts_but_never_clobbers(session):
    r = repo(session)
    # First screen knows the name but not the exchange.
    r.replace_universe((_stock("AAPL", name="Apple Inc."),))
    assert _anchor(session, "AAPL") == ("Apple Inc.", None)

    # A later, nameless screen learns the exchange: the name must survive, exchange fills.
    r.replace_universe((_stock("AAPL", name=None, exchange="NASDAQ"),))
    assert _anchor(session, "AAPL") == ("Apple Inc.", "NASDAQ")

    # A different exchange never overwrites the settled one.
    r.replace_universe((_stock("AAPL", name=None, exchange="NYSE"),))
    assert _anchor(session, "AAPL") == ("Apple Inc.", "NASDAQ")


def test_search_matches_ticker_or_name_case_insensitively(session):
    r = repo(session)
    r.replace_universe(
        (
            _stock("AAPL", name="Apple Inc."),
            _stock("MSFT", name="Microsoft Corp"),
        )
    )

    assert [s.ticker for s in r.search("apple", limit=10)] == ["AAPL"]  # by name
    assert [s.ticker for s in r.search("msft", limit=10)] == ["MSFT"]  # by ticker
    assert [s.ticker for s in r.search("corp", limit=10)] == ["MSFT"]  # by name
    assert r.search("nomatch", limit=10) == ()


def test_search_orders_by_market_cap_desc_and_respects_limit(session):
    r = repo(session)
    r.replace_universe(
        (
            _stock("SML", name="Small Corp", market_cap=5e11),
            _stock("BIG", name="Big Corp", market_cap=2e12),
        )
    )

    hits = r.search("corp", limit=10)
    assert [s.ticker for s in hits] == ["BIG", "SML"]  # largest first
    assert isinstance(hits[0], ScreenedStock)
    assert [s.ticker for s in r.search("corp", limit=1)] == ["BIG"]  # limit respected


def test_search_on_empty_universe_is_empty(session):
    assert repo(session).search("anything", limit=10) == ()
