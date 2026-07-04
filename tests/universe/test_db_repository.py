"""Tests for the database-backed UniverseRepository.

Offline: an in-memory SQLite database stands in for the real ``stocks`` table (the universe
has no table of its own). Verifies the additive upsert (insert new / refresh in place /
never remove an absent member), the fill-but-don't-clobber rule for the anchor's
name/exchange/sector, the screen stamp, added-vs-updated counting, and the enrichment pass's
read/write of the sector/industry classification.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.stocks.models import StockRecord, get_or_create_stock
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.entities import CompanyClassification, ScreenedStock

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


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


def _row(session, ticker) -> StockRecord:
    return session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one()


def _screened_count(session) -> int:
    """Anchors marked as screened members (a ``market_cap`` is set)."""
    return session.execute(
        select(func.count())
        .select_from(StockRecord)
        .where(StockRecord.market_cap.is_not(None))
    ).scalar_one()


def test_upsert_inserts_new_members_fills_the_anchor_and_stamps(session):
    counts = repo(session).upsert_screen(
        (
            _stock(
                "AAPL",
                name="Apple Inc.",
                exchange="NASDAQ",
                market_cap=3e12,
                sector="Technology",
            ),
            _stock("XOM", name="Exxon Mobil", market_cap=5e11),
        )
    )

    assert (counts.added, counts.updated) == (2, 0)
    assert _screened_count(session) == 2
    aapl = _row(session, "AAPL")
    assert (aapl.name, aapl.exchange, aapl.market_cap, aapl.sector) == (
        "Apple Inc.",
        "NASDAQ",
        3e12,
        "Technology",
    )
    # The screen time is stamped on the anchor (SQLite hands it back naive).
    assert aapl.screened_at.replace(tzinfo=timezone.utc) == _NOW
    xom = _row(session, "XOM")
    assert (xom.name, xom.exchange, xom.sector) == ("Exxon Mobil", None, None)


def test_upsert_refreshes_market_cap_in_place(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3.0e12),))
    counts = r.upsert_screen((_stock("AAPL", market_cap=3.4e12),))

    assert (counts.added, counts.updated) == (0, 1)
    assert _screened_count(session) == 1  # refreshed, not duplicated
    assert _row(session, "AAPL").market_cap == 3.4e12


def test_upsert_is_additive_absent_members_are_kept(session):
    r = repo(session)
    r.upsert_screen(
        (_stock("AAPL", market_cap=3e12), _stock("XOM", market_cap=5e11))
    )
    # A later screen no longer lists XOM (fell below the floor / delisted).
    counts = r.upsert_screen((_stock("AAPL", market_cap=3.1e12),))

    assert (counts.added, counts.updated) == (0, 1)
    # XOM is NOT removed — the sync is additive; its last-screened cap survives.
    assert _screened_count(session) == 2
    assert _row(session, "XOM").market_cap == 5e11


def test_upsert_fills_missing_anchor_facts_but_never_clobbers(session):
    r = repo(session)
    # First screen knows the name but not the exchange/sector.
    r.upsert_screen((_stock("AAPL", name="Apple Inc.", market_cap=3e12),))
    aapl = _row(session, "AAPL")
    assert (aapl.name, aapl.exchange, aapl.sector) == ("Apple Inc.", None, None)

    # A later, nameless screen learns the exchange + sector: the name survives, they fill.
    r.upsert_screen(
        (
            _stock(
                "AAPL", name=None, exchange="NASDAQ", sector="Technology", market_cap=3e12
            ),
        )
    )
    aapl = _row(session, "AAPL")
    assert (aapl.name, aapl.exchange, aapl.sector) == (
        "Apple Inc.",
        "NASDAQ",
        "Technology",
    )

    # A different exchange/sector never overwrites the settled ones.
    r.upsert_screen(
        (_stock("AAPL", exchange="NYSE", sector="Energy", market_cap=3e12),)
    )
    aapl = _row(session, "AAPL")
    assert (aapl.exchange, aapl.sector) == ("NASDAQ", "Technology")


def test_upsert_counts_a_preexisting_unscreened_anchor_as_added(session):
    # A ticker the app already knows (e.g. from a ticker-card lookup), never screened.
    get_or_create_stock(session, "AAPL", "Apple Inc.")
    session.commit()

    counts = repo(session).upsert_screen(
        (_stock("AAPL", market_cap=3e12, exchange="NASDAQ"),)
    )
    # First time it's screened => added, not updated (screened_at was null).
    assert (counts.added, counts.updated) == (1, 0)
    assert _row(session, "AAPL").market_cap == 3e12


def test_tickers_missing_industry_lists_unclassified_ordered_and_capped(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", market_cap=3e12),
            _stock("MSFT", market_cap=2e12),
            _stock("XOM", market_cap=5e11),
        )
    )
    # A non-screened, incidentally-known ticker counts too — the work-list spans the whole
    # stocks table, not only screened members.
    get_or_create_stock(session, "TSLA", None)
    session.commit()
    # Classify one so it drops out of the work-list.
    r.set_classification(
        "MSFT", CompanyClassification(sector="technology", industry="software_infrastructure")
    )

    # Ascending by ticker, and capped to the limit.
    assert r.tickers_missing_industry(10) == ("AAPL", "TSLA", "XOM")
    assert r.tickers_missing_industry(2) == ("AAPL", "TSLA")


def test_set_classification_fills_both_sides(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))

    r.set_classification(
        "AAPL", CompanyClassification(sector="technology", industry="consumer_electronics")
    )

    aapl = _row(session, "AAPL")
    assert (aapl.sector, aapl.industry) == ("technology", "consumer_electronics")


def test_set_classification_is_fill_once_and_one_sided(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))

    # First run only knows the industry (Yahoo gave no sector).
    r.set_classification("AAPL", CompanyClassification(industry="consumer_electronics"))
    aapl = _row(session, "AAPL")
    assert (aapl.sector, aapl.industry) == (None, "consumer_electronics")

    # A later run fills the still-missing sector but never overwrites the settled industry.
    r.set_classification(
        "AAPL", CompanyClassification(sector="technology", industry="something_else")
    )
    aapl = _row(session, "AAPL")
    assert (aapl.sector, aapl.industry) == ("technology", "consumer_electronics")


def test_set_classification_ignores_an_unknown_ticker(session):
    # No row for NOPE — a no-op: no row is created and nothing raises.
    repo(session).set_classification("NOPE", CompanyClassification(industry="x"))
    assert (
        session.execute(
            select(StockRecord).where(StockRecord.ticker == "NOPE")
        ).scalar_one_or_none()
        is None
    )
