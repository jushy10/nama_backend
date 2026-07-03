"""Tests for the database-backed RecommendationsRepository.

Offline: an in-memory SQLite database stands in for the real table. Verifies the
round-trip (entities -> rows -> entities) including the canonical newest-first order, the
*merge* on upsert (fetched months replaced, earlier months kept — the accumulation that
outlives Yahoo's ~4-month window), the parent ``stocks`` row + name fill-but-don't-clobber,
a clean miss, and the last-refresh (max fetch stamp) ordering of refresh targets.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.recommendations.db_repository import SqlRecommendationsRepository
from app.stocks.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)
from app.stocks.recommendations.models import (
    StockRecommendationTrendRecord,
    StockRecord,
)

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlRecommendationsRepository:
    return SqlRecommendationsRepository(session, now=lambda: _NOW)


def _a_trend(period: date, *, buy=0, **counts) -> RecommendationTrend:
    return RecommendationTrend(
        period=period,
        strong_buy=counts.get("strong_buy", 0),
        buy=buy,
        hold=counts.get("hold", 0),
        sell=counts.get("sell", 0),
        strong_sell=counts.get("strong_sell", 0),
    )


def _a_run(*trends: RecommendationTrend, symbol="AAPL") -> AnalystRecommendations:
    return AnalystRecommendations(symbol=symbol, trends=tuple(trends))


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_the_run_newest_first(session):
    r = repo(session)
    # Inserted oldest-first on purpose — the read must return the canonical order.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _a_run(
            _a_trend(date(2026, 4, 1), buy=20, hold=10),
            _a_trend(date(2026, 5, 1), buy=22, hold=9),
            _a_trend(date(2026, 6, 1), strong_buy=13, buy=24, hold=7),
        ),
    )

    recs = r.get("AAPL")
    assert isinstance(recs, AnalystRecommendations)
    assert [t.period for t in recs.trends] == [
        date(2026, 6, 1),
        date(2026, 5, 1),
        date(2026, 4, 1),
    ]
    latest = recs.latest
    assert (latest.strong_buy, latest.buy, latest.hold) == (13, 24, 7)
    assert latest.total == 44 and latest.consensus == "Buy"


def test_upsert_stamps_the_fetch_time(session):
    # fetched_at isn't part of the read shape, but the cron's stalest-first refresh
    # orders by it — verify the stamp lands on the rows. SQLite hands the timestamp back
    # naive (Postgres keeps the zone); normalize to UTC.
    repo(session).upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=5)))
    stamp = (
        session.execute(select(StockRecommendationTrendRecord.fetched_at))
        .scalars()
        .first()
    )
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_upsert_merges_replacing_fetched_months_and_keeping_earlier_ones(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _a_run(_a_trend(date(2026, 5, 1), buy=20), _a_trend(date(2026, 6, 1), buy=22)),
    )
    # A month later Yahoo's window has moved on: June is revised, July is new, May has
    # rolled out of the window. May must survive; June must carry the revised counts.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _a_run(_a_trend(date(2026, 6, 1), buy=25), _a_trend(date(2026, 7, 1), buy=27)),
    )

    recs = r.get("AAPL")
    assert [t.period for t in recs.trends] == [
        date(2026, 7, 1),
        date(2026, 6, 1),
        date(2026, 5, 1),  # kept from the first fetch — the accumulated history
    ]
    assert recs.trends[1].buy == 25  # June replaced, not duplicated
    rows = session.execute(
        select(func.count()).select_from(StockRecommendationTrendRecord)
    ).scalar_one()
    assert rows == 3


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=5)))
    r.upsert(
        "MSFT", "Microsoft", _a_run(_a_trend(date(2026, 6, 1), buy=9), symbol="MSFT")
    )

    r.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=6)))

    assert r.get("MSFT").latest.buy == 9  # MSFT survived AAPL's refresh


def test_creates_the_parent_stock_row(session):
    repo(session).upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=5)))
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc." and stock.id is not None


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    run = _a_run(_a_trend(date(2026, 6, 1), buy=5))
    r.upsert("AAPL", None, run)
    assert session.execute(
        select(StockRecord.name).where(StockRecord.ticker == "AAPL")
    ).scalar_one() is None

    r.upsert("AAPL", "Apple Inc.", run)
    r.upsert("AAPL", None, run)  # a nameless refresh must not erase it
    assert session.execute(
        select(StockRecord.name).where(StockRecord.ticker == "AAPL")
    ).scalar_one() == "Apple Inc."


def test_refresh_targets_orders_by_last_refresh_not_oldest_row(session):
    # The merge keeps old months' stamps forever, so staleness must read the *newest*
    # stamp (the last refresh): AAPL holds an ancient accumulated month but was refreshed
    # after MSFT, so MSFT is the staler of the two.
    ancient = SqlRecommendationsRepository(session, now=lambda: _NOW - timedelta(days=90))
    mid = SqlRecommendationsRepository(session, now=lambda: _NOW - timedelta(days=10))
    fresh = SqlRecommendationsRepository(session, now=lambda: _NOW)

    ancient.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 4, 1), buy=5)))
    mid.upsert("MSFT", "Microsoft", _a_run(_a_trend(date(2026, 6, 1), buy=9), symbol="MSFT"))
    fresh.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=6)))

    targets = fresh.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "AAPL"]  # last-refreshed last
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert fresh.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected
