from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.domains.coverage.news.db_repository import (
    _MAX_STORED_ARTICLES,
    DbNewsRepository,
)
from app.domains.coverage.news.entities import NewsArticle, StockNews
from app.domains.coverage.news.models import (
    StockNewsRecord,
    StockRecord,
    get_or_create_stock,
)

_NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> DbNewsRepository:
    return DbNewsRepository(session, now=lambda: _NOW)


def _article(article_id: str, *, published: datetime, title="Headline", **kw) -> NewsArticle:
    return NewsArticle(id=article_id, title=title, published_at=published, **kw)


def _run(*articles: NewsArticle, symbol="AAPL") -> StockNews:
    return StockNews(symbol=symbol, articles=tuple(articles))


def _d(day: int) -> datetime:
    return datetime(2026, 6, day, tzinfo=timezone.utc)


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_the_run_newest_first(session):
    r = repo(session)
    # Inserted oldest-first on purpose — the read must return the canonical order.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _run(
            _article("a1", published=_d(1), title="Oldest"),
            _article("a2", published=_d(5), title="Middle", publisher="Reuters"),
            _article("a3", published=_d(9), title="Newest", link="https://x/3"),
        ),
    )

    news = r.get("AAPL")
    assert isinstance(news, StockNews)
    assert [a.id for a in news.articles] == ["a3", "a2", "a1"]
    latest = news.latest
    assert latest.title == "Newest" and latest.link == "https://x/3"
    assert news.articles[1].publisher == "Reuters"


def test_upsert_stamps_the_fetch_time(session):
    # fetched_at isn't part of the read shape, but the cron's stalest-first refresh
    # orders by it — verify the stamp lands on the rows. SQLite hands the timestamp back
    # naive (Postgres keeps the zone); normalize to UTC.
    repo(session).upsert("AAPL", "Apple Inc.", _run(_article("a1", published=_d(1))))
    stamp = session.execute(select(StockNewsRecord.fetched_at)).scalars().first()
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_preserves_all_article_fields(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _run(
            _article(
                "a1",
                published=_d(3),
                title="Deal",
                publisher="Bloomberg",
                link="https://x/1",
                summary="A blurb.",
                content_type="VIDEO",
                thumbnail_url="https://img/1.jpg",
            )
        ),
    )
    a = r.get("AAPL").latest
    assert (a.publisher, a.link, a.summary) == ("Bloomberg", "https://x/1", "A blurb.")
    assert a.content_type == "VIDEO" and a.is_video is True
    assert a.thumbnail_url == "https://img/1.jpg"


def test_upsert_merges_replacing_fetched_articles_and_keeping_earlier_ones(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _run(_article("a1", published=_d(1)), _article("a2", published=_d(5), title="v1")),
    )
    # A later fetch: a2 is re-served (revised title), a3 is new, a1 has scrolled out of
    # Yahoo's window. a1 must survive; a2 must carry the revised title, not duplicate.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _run(_article("a2", published=_d(5), title="v2"), _article("a3", published=_d(9))),
    )

    news = r.get("AAPL")
    assert [a.id for a in news.articles] == ["a3", "a2", "a1"]  # a1 kept — accumulated
    assert news.articles[1].title == "v2"  # a2 replaced, not duplicated
    rows = session.execute(
        select(func.count()).select_from(StockNewsRecord)
    ).scalar_one()
    assert rows == 3


def test_upsert_prunes_the_feed_to_the_retention_cap(session):
    # One fetch carrying more than the cap: the store keeps only the newest N, dropping the
    # oldest — so the higher-volume news history stays bounded.
    overflow = _MAX_STORED_ARTICLES + 5
    articles = [
        _article(f"a{i}", published=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i))
        for i in range(overflow)
    ]
    repo(session).upsert("AAPL", "Apple Inc.", _run(*articles))

    news = repo(session).get("AAPL")
    assert len(news.articles) == _MAX_STORED_ARTICLES
    # The newest article (highest day) is kept; the 5 oldest were pruned.
    assert news.latest.id == f"a{overflow - 1}"
    kept_ids = {a.id for a in news.articles}
    assert "a0" not in kept_ids and "a4" not in kept_ids
    assert f"a{overflow - _MAX_STORED_ARTICLES}" in kept_ids  # first survivor


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _run(_article("a1", published=_d(1))))
    r.upsert("MSFT", "Microsoft", _run(_article("m1", published=_d(1)), symbol="MSFT"))

    r.upsert("AAPL", "Apple Inc.", _run(_article("a2", published=_d(2))))

    assert [a.id for a in r.get("MSFT").articles] == ["m1"]  # MSFT survived AAPL's refresh


def test_creates_the_parent_stock_row(session):
    repo(session).upsert("AAPL", "Apple Inc.", _run(_article("a1", published=_d(1))))
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc." and stock.id is not None


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    run = _run(_article("a1", published=_d(1)))
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
    # The merge keeps old articles' stamps forever, so staleness must read the *newest*
    # stamp (the last refresh): AAPL holds an ancient accumulated article but was refreshed
    # after MSFT, so MSFT is the staler of the two.
    ancient = DbNewsRepository(session, now=lambda: _NOW - timedelta(days=90))
    mid = DbNewsRepository(session, now=lambda: _NOW - timedelta(days=10))
    fresh = DbNewsRepository(session, now=lambda: _NOW)

    ancient.upsert("AAPL", "Apple Inc.", _run(_article("a1", published=_d(1))))
    mid.upsert("MSFT", "Microsoft", _run(_article("m1", published=_d(5)), symbol="MSFT"))
    fresh.upsert("AAPL", "Apple Inc.", _run(_article("a2", published=_d(9))))

    targets = fresh.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "AAPL"]  # last-refreshed last
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert fresh.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected


def test_refresh_targets_seeds_uncached_anchor_stocks_first(session):
    # A stock in the anchor with no article rows yet (e.g. added by the universe sync) is a
    # *seed* target — returned ahead of any cached stock so a sweep fills new coverage first.
    r = repo(session)
    r.upsert("MSFT", "Microsoft", _run(_article("m1", published=_d(5)), symbol="MSFT"))
    get_or_create_stock(session, "NEWCO", "New Co")  # anchor only, never fetched
    session.commit()

    targets = r.refresh_targets(None)  # None => every anchor stock
    assert [t.symbol for t in targets] == ["NEWCO", "MSFT"]  # un-cached seeded first
    assert dict(targets)["NEWCO"] == "New Co"  # carries the anchor name
