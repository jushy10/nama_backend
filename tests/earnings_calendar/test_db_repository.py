"""Tests for the database-backed EarningsCalendarRepository.

Offline: an in-memory SQLite database seeded with a few ``stocks`` anchors + their
``stock_quarterly_earnings`` rows. Verifies the cross-table read: only *upcoming* quarters
(``eps_actual IS NULL``) with a scheduled ``report_date`` inside the window, joined to
name + sector, ordered by date then ticker, capped by ``limit``. A reported quarter, a
dateless upcoming quarter, and an out-of-window one are all excluded.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.earnings.quarterly.entities import EarningsSession
from app.stocks.earnings.quarterly.models import StockQuarterlyEarningsRecord
from app.stocks.earnings_calendar.db_repository import SqlEarningsCalendarRepository
from app.stocks.stocks.models import get_or_create_stock

_FETCHED = datetime(2026, 7, 14, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _stock(session, ticker, name, sector, market_cap=None):
    stock = get_or_create_stock(session, ticker, name)
    stock.sector = sector
    stock.market_cap = market_cap
    session.flush()
    return stock


def _quarter(
    session,
    stock,
    *,
    year,
    quarter,
    report_date,
    eps_actual=None,
    eps_estimate=1.0,
    report_session=None,
):
    session.add(
        StockQuarterlyEarningsRecord(
            stock_id=stock.id,
            fiscal_year=year,
            fiscal_quarter=quarter,
            report_date=report_date,
            eps_actual=eps_actual,
            eps_estimate=eps_estimate,
            report_session=report_session,
            fetched_at=_FETCHED,
        )
    )


def _seed(session):
    aapl = _stock(session, "AAPL", "Apple", "technology", market_cap=3.4e12)
    msft = _stock(session, "MSFT", "Microsoft", "technology")
    nvda = _stock(session, "NVDA", "NVIDIA", "technology")
    # In-window upcoming reports (eps_actual is None).
    _quarter(
        session, msft, year=2026, quarter=3, report_date=date(2026, 7, 20),
        report_session="bmo",
    )
    _quarter(
        session, aapl, year=2026, quarter=3, report_date=date(2026, 7, 20),
        report_session="amc",
    )
    # NVDA's session left NULL (a pre-column / no-time row) → reads back as UNKNOWN.
    _quarter(session, nvda, year=2026, quarter=3, report_date=date(2026, 7, 25))
    # Excluded: already reported (eps_actual set), even though the date is in-window.
    _quarter(
        session, aapl, year=2026, quarter=2, report_date=date(2026, 7, 22), eps_actual=1.4
    )
    # Excluded: upcoming but no scheduled date.
    _quarter(session, msft, year=2026, quarter=4, report_date=None)
    # Excluded: out of the window.
    _quarter(session, nvda, year=2026, quarter=4, report_date=date(2026, 9, 1))
    session.commit()


def repo(session) -> SqlEarningsCalendarRepository:
    return SqlEarningsCalendarRepository(session)


def test_returns_upcoming_reports_in_window_ordered_by_date_then_ticker(session):
    _seed(session)
    items = repo(session).upcoming(date(2026, 7, 15), date(2026, 7, 31), 100)

    # AAPL + MSFT (same day, alphabetical) then NVDA — reported / dateless / out-of-window excluded.
    assert [(i.ticker, i.report_date) for i in items] == [
        ("AAPL", date(2026, 7, 20)),
        ("MSFT", date(2026, 7, 20)),
        ("NVDA", date(2026, 7, 25)),
    ]


def test_joins_name_and_sector(session):
    _seed(session)
    items = repo(session).upcoming(date(2026, 7, 15), date(2026, 7, 31), 100)
    aapl = next(i for i in items if i.ticker == "AAPL")
    assert aapl.name == "Apple"
    assert aapl.sector == "technology"


def test_joins_market_cap(session):
    _seed(session)
    items = repo(session).upcoming(date(2026, 7, 15), date(2026, 7, 31), 100)
    by_ticker = {i.ticker: i.market_cap for i in items}
    assert by_ticker["AAPL"] == 3.4e12
    # A not-yet-screened symbol has no market cap → None (no highlight downstream).
    assert by_ticker["NVDA"] is None


def test_maps_the_report_session(session):
    _seed(session)
    items = repo(session).upcoming(date(2026, 7, 15), date(2026, 7, 31), 100)
    by_ticker = {i.ticker: i.session for i in items}
    assert by_ticker["AAPL"] is EarningsSession.AMC
    assert by_ticker["MSFT"] is EarningsSession.BMO
    assert by_ticker["NVDA"] is EarningsSession.UNKNOWN  # NULL column → UNKNOWN


def test_window_bounds_are_inclusive(session):
    _seed(session)
    # A window that exactly straddles the two report dates includes both ends.
    items = repo(session).upcoming(date(2026, 7, 20), date(2026, 7, 25), 100)
    assert {i.ticker for i in items} == {"AAPL", "MSFT", "NVDA"}


def test_respects_the_limit(session):
    _seed(session)
    items = repo(session).upcoming(date(2026, 7, 15), date(2026, 7, 31), 1)
    assert len(items) == 1
    assert items[0].ticker == "AAPL"  # first by (date, ticker)


def test_empty_when_nothing_scheduled(session):
    _seed(session)
    assert repo(session).upcoming(date(2026, 8, 1), date(2026, 8, 31), 100) == []
