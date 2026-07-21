from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.entities import StockPerformance
from app.stocks.catalog.performance.performance_repository_adapter_impl import PerformanceRepositoryAdapterImpl
from app.stocks.catalog.anchor.models import StockRecord

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _repo(session, *, now=_NOW) -> PerformanceRepositoryAdapterImpl:
    return PerformanceRepositoryAdapterImpl(session, now=lambda: now)


def _add(session, ticker, *, market_cap=1e10, synced_at=None):
    session.add(
        StockRecord(ticker=ticker, market_cap=market_cap, performance_synced_at=synced_at)
    )
    session.commit()


def _row(session, ticker) -> StockRecord:
    return session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one()


def _perf(one_year=None, **windows):
    return StockPerformance(
        one_week=windows.get("one_week"),
        one_month=windows.get("one_month"),
        three_month=windows.get("three_month"),
        six_month=windows.get("six_month"),
        ytd=windows.get("ytd"),
        one_year=one_year,
    )


def test_refresh_targets_unsynced_first_then_stalest(session):
    day = timedelta(days=1)
    _add(session, "FRESH", synced_at=_NOW)
    _add(session, "STALE", synced_at=_NOW - 5 * day)
    _add(session, "NEVER", synced_at=None)

    targets = _repo(session).refresh_targets(None)

    # NEVER (un-synced) leads, then STALE (older), then FRESH (newest).
    assert targets == ("NEVER", "STALE", "FRESH")


def test_refresh_targets_excludes_unscreened_rows(session):
    _add(session, "SCREENED", market_cap=1e10)
    _add(session, "INCIDENTAL", market_cap=None)  # a ticker-card lookup, never screened

    assert _repo(session).refresh_targets(None) == ("SCREENED",)


def test_refresh_targets_respects_the_limit(session):
    _add(session, "A", synced_at=None)
    _add(session, "B", synced_at=None)
    _add(session, "C", synced_at=None)

    assert len(_repo(session).refresh_targets(2)) == 2


def test_set_performance_writes_windows_and_stamp(session):
    _add(session, "NVDA", synced_at=None)

    written = _repo(session).set_performance(
        {"NVDA": _perf(one_year=120.0, one_week=2.0, ytd=40.0)}
    )

    assert written == 1
    row = _row(session, "NVDA")
    assert row.perf_one_year == 120.0
    assert row.perf_one_week == 2.0
    assert row.perf_ytd == 40.0
    # SQLite drops tzinfo on the DateTime round-trip, so compare tz-naively — the point is the
    # injected clock landed on the stamp.
    assert row.performance_synced_at.replace(tzinfo=None) == _NOW.replace(tzinfo=None)


def test_set_performance_overwrites_including_to_none(session):
    _add(session, "NVDA", synced_at=None)
    repo = _repo(session)
    repo.set_performance({"NVDA": _perf(one_year=120.0, one_week=2.0)})

    # A later run where the 1W window lost enough history clears it (moving snapshot).
    repo.set_performance({"NVDA": _perf(one_year=130.0)})

    row = _row(session, "NVDA")
    assert row.perf_one_year == 130.0
    assert row.perf_one_week is None


def test_set_performance_skips_a_ticker_with_no_anchor_row(session):
    _add(session, "NVDA", synced_at=None)

    written = _repo(session).set_performance(
        {"NVDA": _perf(one_year=120.0), "GHOST": _perf(one_year=5.0)}
    )

    assert written == 1  # GHOST has no row -> not counted, not created
    assert (
        session.execute(
            select(StockRecord).where(StockRecord.ticker == "GHOST")
        ).scalar_one_or_none()
        is None
    )
