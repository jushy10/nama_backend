from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.domains.coverage.recommendations.db_repository import (
    DbRatingChangesRepository,
    DbRecommendationsRepository,
)
from app.domains.coverage.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    RatingChange,
    RecommendationTrend,
)
from app.domains.coverage.recommendations.models import (
    StockAnalystRatingChangeRecord,
    StockRecommendationTrendRecord,
    StockRecord,
    get_or_create_stock,
)

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> DbRecommendationsRepository:
    return DbRecommendationsRepository(session, now=lambda: _NOW)


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
    ancient = DbRecommendationsRepository(session, now=lambda: _NOW - timedelta(days=90))
    mid = DbRecommendationsRepository(session, now=lambda: _NOW - timedelta(days=10))
    fresh = DbRecommendationsRepository(session, now=lambda: _NOW)

    ancient.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 4, 1), buy=5)))
    mid.upsert("MSFT", "Microsoft", _a_run(_a_trend(date(2026, 6, 1), buy=9), symbol="MSFT"))
    fresh.upsert("AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=6)))

    targets = fresh.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "AAPL"]  # last-refreshed last
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert fresh.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected


def test_refresh_targets_seeds_uncached_anchor_stocks_first(session):
    # A stock in the anchor with no trend rows yet (e.g. added by the universe sync) is a
    # *seed* target — returned ahead of any cached stock so a sweep fills new coverage first.
    r = repo(session)
    r.upsert("MSFT", "Microsoft", _a_run(_a_trend(date(2026, 6, 1), buy=9), symbol="MSFT"))
    get_or_create_stock(session, "NEWCO", "New Co")  # anchor only, never fetched
    session.commit()

    targets = r.refresh_targets(None)  # None => every anchor stock
    assert [t.symbol for t in targets] == ["NEWCO", "MSFT"]  # un-cached seeded first
    assert dict(targets)["NEWCO"] == "New Co"  # carries the anchor name


def _targets(**kw) -> AnalystPriceTargets:
    return AnalystPriceTargets(
        mean=kw.get("mean"),
        high=kw.get("high"),
        low=kw.get("low"),
        median=kw.get("median"),
    )


def test_stores_price_targets_on_the_latest_row_only_and_reads_them_back(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        AnalystRecommendations(
            "AAPL",
            (
                _a_trend(date(2026, 5, 1), buy=20),
                _a_trend(date(2026, 6, 1), buy=22),  # the latest month
            ),
            price_targets=_targets(mean=315.5, high=400.0, low=215.0, median=315.0),
        ),
    )

    # The read surfaces the target block off the newest row.
    recs = r.get("AAPL")
    assert recs.price_targets == _targets(mean=315.5, high=400.0, low=215.0, median=315.0)

    # In the DB the target lands on June (latest) only; May is left null.
    rows = {
        row.period: row
        for row in session.execute(select(StockRecommendationTrendRecord)).scalars()
    }
    assert rows[date(2026, 6, 1)].target_mean == 315.5
    assert rows[date(2026, 5, 1)].target_mean is None


def test_missing_price_targets_read_back_as_none(session):
    # A run with no targets (the common case) stores nulls and reads back price_targets=None,
    # never an empty-but-present block.
    repo(session).upsert(
        "AAPL", "Apple Inc.", _a_run(_a_trend(date(2026, 6, 1), buy=5))
    )
    assert repo(session).get("AAPL").price_targets is None


_RC_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def rc_repo(session, *, now=_RC_NOW) -> DbRatingChangesRepository:
    return DbRatingChangesRepository(session, now=lambda: now)


def _a_change(firm: str, published_at: date, **kw) -> RatingChange:
    return RatingChange(
        firm=firm,
        published_at=published_at,
        action=kw.get("action"),
        from_grade=kw.get("from_grade"),
        to_grade=kw.get("to_grade"),
        target_current=kw.get("target_current"),
        target_prior=kw.get("target_prior"),
    )


def _a_changes(*changes: RatingChange, symbol="AAPL") -> AnalystRatingChanges:
    return AnalystRatingChanges(symbol=symbol, changes=tuple(changes))


def test_rating_changes_get_on_empty_table_is_a_miss(session):
    assert rc_repo(session).get("AAPL") is None


def test_rating_changes_roundtrip_newest_first(session):
    rc_repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        _a_changes(
            _a_change("Older Firm", date(2026, 5, 1), action="down", to_grade="Hold"),
            _a_change(
                "TD Cowen",
                date(2026, 6, 9),
                action="main",
                to_grade="Buy",
                target_current=350.0,
                target_prior=335.0,
            ),
        ),
    )

    changes = rc_repo(session).get("AAPL")
    assert isinstance(changes, AnalystRatingChanges)
    assert [c.published_at for c in changes.changes] == [
        date(2026, 6, 9),
        date(2026, 5, 1),
    ]
    latest = changes.latest
    assert latest.firm == "TD Cowen" and latest.to_grade == "Buy"
    assert (latest.target_current, latest.target_prior) == (350.0, 335.0)


def test_rating_changes_upsert_is_insert_only_and_accumulates(session):
    r = rc_repo(session)
    r.upsert("AAPL", "Apple Inc.", _a_changes(_a_change("Firm A", date(2026, 5, 1))))
    # A later run re-serves the same event plus a new one: the duplicate is skipped, the new
    # one is added, and the history accumulates.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _a_changes(
            _a_change("Firm A", date(2026, 5, 1)),  # already stored — skipped
            _a_change("Firm B", date(2026, 6, 2), action="up"),
        ),
    )

    changes = r.get("AAPL")
    assert [(c.firm, c.published_at) for c in changes.changes] == [
        ("Firm B", date(2026, 6, 2)),
        ("Firm A", date(2026, 5, 1)),
    ]
    rows = session.execute(
        select(func.count()).select_from(StockAnalystRatingChangeRecord)
    ).scalar_one()
    assert rows == 2  # not 3 — the duplicate wasn't re-inserted


def test_rating_changes_creates_parent_and_fills_name(session):
    rc_repo(session).upsert(
        "AAPL", "Apple Inc.", _a_changes(_a_change("Firm A", date(2026, 6, 1)))
    )
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc." and stock.id is not None
