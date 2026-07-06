"""Tests for the database-backed ETF repositories.

Offline: an in-memory SQLite database stands in for the real ``etfs`` table. Two suites:

- ``SqlEtfRepository`` (write side): the additive upsert (insert new / refresh in place / never
  remove an absent fund), the fill-but-don't-clobber rule for name/exchange, the screen stamp,
  added-vs-updated counting, and the category enrichment pass's read/write.
- ``SqlEtfSearchRepository`` (read side): the name-or-ticker substring match, the category filter,
  the sorts (net assets / expense ratio, nulls last, stable ticker tiebreak), limit/offset paging
  with a total count, and the distinct category menu.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.etfs.db_repository import SqlEtfRepository, SqlEtfSearchRepository
from app.stocks.etfs.entities import (
    EtfClassification,
    EtfSearchCriteria,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.etfs.models import EtfRecord, get_or_create_etf

_NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=_NOW) -> SqlEtfRepository:
    return SqlEtfRepository(session, now=lambda: now)


def _etf(ticker, *, name=None, exchange=None, net_assets=1e10, expense_ratio=0.2):
    return ScreenedEtf(
        ticker=ticker,
        name=name,
        exchange=exchange,
        net_assets=net_assets,
        expense_ratio=expense_ratio,
    )


def _row(session, ticker) -> EtfRecord:
    return session.execute(
        select(EtfRecord).where(EtfRecord.ticker == ticker)
    ).scalar_one()


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(EtfRecord)).scalar_one()


def test_upsert_inserts_new_funds_fills_the_row_and_stamps(session):
    counts = repo(session).upsert_screen(
        (
            _etf(
                "SPY",
                name="SPDR S&P 500 ETF Trust",
                exchange="NYSE",
                net_assets=5e11,
                expense_ratio=0.09,
            ),
            _etf("QQQ", name="Invesco QQQ Trust", net_assets=3e11),
        )
    )

    assert (counts.added, counts.updated) == (2, 0)
    assert _count(session) == 2
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange, spy.net_assets, spy.expense_ratio) == (
        "SPDR S&P 500 ETF Trust",
        "NYSE",
        5e11,
        0.09,
    )
    # The screen time is stamped on the row (SQLite hands it back naive); category is untouched
    # by the screen upsert (the enrichment pass owns it).
    assert spy.screened_at.replace(tzinfo=timezone.utc) == _NOW
    assert spy.category is None


def test_upsert_refreshes_figures_in_place(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5.0e11, expense_ratio=0.09),))
    counts = r.upsert_screen((_etf("SPY", net_assets=5.4e11, expense_ratio=0.10),))

    assert (counts.added, counts.updated) == (0, 1)
    assert _count(session) == 1  # refreshed, not duplicated
    spy = _row(session, "SPY")
    assert (spy.net_assets, spy.expense_ratio) == (5.4e11, 0.10)


def test_upsert_preserves_an_enriched_category(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5e11),))
    r.set_category("SPY", EtfClassification(category="large_blend"))
    # A later screen refresh must not wipe the enriched category.
    r.upsert_screen((_etf("SPY", net_assets=5.1e11),))
    assert _row(session, "SPY").category == "large_blend"


def test_upsert_is_additive_absent_funds_are_kept(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5e11), _etf("ARKK", net_assets=8e9)))
    # A later screen no longer lists ARKK (dropped out of the top set).
    counts = r.upsert_screen((_etf("SPY", net_assets=5.1e11),))

    assert (counts.added, counts.updated) == (0, 1)
    # ARKK is NOT removed — the sync is additive; its last-screened figures survive.
    assert _count(session) == 2
    assert _row(session, "ARKK").net_assets == 8e9


def test_upsert_fills_missing_name_and_exchange_but_never_clobbers(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", name="SPDR S&P 500", exchange=None),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", None)

    # A later, nameless screen learns the exchange: the name survives, the exchange fills.
    r.upsert_screen((_etf("SPY", name=None, exchange="NYSE"),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", "NYSE")

    # A different name/exchange never overwrites the settled ones.
    r.upsert_screen((_etf("SPY", name="Something Else", exchange="NASDAQ"),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", "NYSE")


def test_upsert_counts_a_preexisting_unscreened_row_as_added(session):
    # A fund row that exists but was never screened (screened_at still null).
    get_or_create_etf(session, "SPY", "SPDR S&P 500")
    session.commit()

    counts = repo(session).upsert_screen((_etf("SPY", net_assets=5e11),))
    # First time it's screened => added, not updated.
    assert (counts.added, counts.updated) == (1, 0)
    assert _row(session, "SPY").net_assets == 5e11


def test_tickers_missing_category_lists_uncategorised_by_net_assets_and_capped(session):
    r = repo(session)
    r.upsert_screen(
        (
            _etf("SPY", net_assets=5e11),
            _etf("QQQ", net_assets=3e11),
            _etf("ARKK", net_assets=8e9),
        )
    )
    # Categorise one so it drops out of the work-list.
    r.set_category("QQQ", EtfClassification(category="large_growth"))

    # Largest net_assets first (the megafunds before the tail), capped to the limit.
    assert r.tickers_missing_category(10) == ("SPY", "ARKK")
    assert r.tickers_missing_category(1) == ("SPY",)


def test_set_category_fills_once_and_never_clobbers(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5e11),))

    r.set_category("SPY", EtfClassification(category="large_blend"))
    assert _row(session, "SPY").category == "large_blend"

    # A later run never overwrites the settled category.
    r.set_category("SPY", EtfClassification(category="something_else"))
    assert _row(session, "SPY").category == "large_blend"


def test_set_category_ignores_an_empty_classification_and_unknown_ticker(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5e11),))
    # An empty classification writes nothing.
    r.set_category("SPY", EtfClassification(category=None))
    assert _row(session, "SPY").category is None
    # No row for NOPE — a no-op: nothing is created and nothing raises.
    r.set_category("NOPE", EtfClassification(category="x"))
    assert (
        session.execute(
            select(EtfRecord).where(EtfRecord.ticker == "NOPE")
        ).scalar_one_or_none()
        is None
    )


# --- SqlEtfSearchRepository (the read side) ------------------------------------------------


def _seed(
    session,
    ticker,
    *,
    name=None,
    exchange=None,
    net_assets=1e10,
    expense_ratio=None,
    category=None,
):
    """Insert an ``etfs`` row directly — whatever the sync would have written."""
    session.add(
        EtfRecord(
            ticker=ticker,
            name=name,
            exchange=exchange,
            net_assets=net_assets,
            expense_ratio=expense_ratio,
            category=category,
            screened_at=_NOW,
        )
    )
    session.commit()


def _criteria(**overrides) -> EtfSearchCriteria:
    base = dict(
        query=None,
        category=None,
        sort=EtfSort.NET_ASSETS,
        direction=SortDirection.DESC,
        limit=50,
        offset=0,
    )
    base.update(overrides)
    return EtfSearchCriteria(**base)


def _tickers(page) -> list[str]:
    return [r.ticker for r in page.results]


def test_search_matches_name_or_ticker_substring_case_insensitively(session):
    _seed(session, "GLD", name="SPDR Gold Shares")
    _seed(session, "RING", name="iShares MSCI Global Gold Miners")
    _seed(session, "SPY", name="SPDR S&P 500 ETF Trust")  # matches by ticker only for "spy"
    r = SqlEtfSearchRepository(session)

    assert set(_tickers(r.search(_criteria(query="gold")))) == {"GLD", "RING"}
    assert set(_tickers(r.search(_criteria(query="GOLD")))) == {"GLD", "RING"}
    assert _tickers(r.search(_criteria(query="spy"))) == ["SPY"]


def test_search_treats_like_metacharacters_literally(session):
    _seed(session, "SPY", name="SPDR S&P 500")
    r = SqlEtfSearchRepository(session)
    assert _tickers(r.search(_criteria(query="%"))) == []


def test_search_filters_by_category(session):
    _seed(session, "SPY", category="large_blend")
    _seed(session, "IVV", category="large_blend")
    _seed(session, "QQQ", category="large_growth")
    _seed(session, "GLD", category="commodities_focused")
    r = SqlEtfSearchRepository(session)

    assert set(_tickers(r.search(_criteria(category="large_blend")))) == {"SPY", "IVV"}
    assert _tickers(r.search(_criteria(category="commodities_focused"))) == ["GLD"]
    # The category ANDs with the text filter.
    assert _tickers(
        r.search(_criteria(query="qqq", category="large_growth"))
    ) == ["QQQ"]


def test_search_sorts_by_net_assets_both_directions(session):
    _seed(session, "BIG", net_assets=5e11)
    _seed(session, "MID", net_assets=1e11)
    _seed(session, "SMALL", net_assets=5e9)
    r = SqlEtfSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=EtfSort.NET_ASSETS))) == ["BIG", "MID", "SMALL"]
    assert _tickers(
        r.search(_criteria(sort=EtfSort.NET_ASSETS, direction=SortDirection.ASC))
    ) == ["SMALL", "MID", "BIG"]


def test_search_sorts_by_expense_ratio_with_nulls_last_either_direction(session):
    _seed(session, "AAA", expense_ratio=0.10)
    _seed(session, "BBB", expense_ratio=0.03)
    _seed(session, "CCC", expense_ratio=None)  # unfilled figure sinks to the bottom
    _seed(session, "DDD", expense_ratio=0.75)
    r = SqlEtfSearchRepository(session)

    # Ascending (cheapest first): 0.03, 0.10, 0.75, then the null.
    assert _tickers(
        r.search(_criteria(sort=EtfSort.EXPENSE_RATIO, direction=SortDirection.ASC))
    ) == ["BBB", "AAA", "DDD", "CCC"]
    # Descending: 0.75, 0.10, 0.03, and the null is STILL last (nulls_last, not just reversed).
    assert _tickers(r.search(_criteria(sort=EtfSort.EXPENSE_RATIO))) == [
        "DDD",
        "AAA",
        "BBB",
        "CCC",
    ]


def test_search_breaks_sort_ties_by_ticker(session):
    _seed(session, "TWOB", net_assets=1e11)
    _seed(session, "TWOA", net_assets=1e11)  # same size — ticker decides the order
    r = SqlEtfSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=EtfSort.NET_ASSETS))) == ["TWOA", "TWOB"]


def test_search_paginates_with_a_total_count(session):
    for i, na in enumerate([5e11, 4e11, 3e11, 2e11, 1e11]):
        _seed(session, f"E{5 - i}", net_assets=na)  # E5..E1, biggest first
    r = SqlEtfSearchRepository(session)

    first = r.search(_criteria(limit=2, offset=0))
    assert (_tickers(first), first.total, first.limit, first.offset) == (
        ["E5", "E4"],
        5,
        2,
        0,
    )
    assert _tickers(r.search(_criteria(limit=2, offset=2))) == ["E3", "E2"]
    last = r.search(_criteria(limit=2, offset=4))
    assert (_tickers(last), last.total) == (["E1"], 5)


def test_search_maps_every_row_field(session):
    _seed(
        session,
        "SPY",
        name="SPDR S&P 500 ETF Trust",
        exchange="NYSE",
        net_assets=5e11,
        expense_ratio=0.09,
        category="large_blend",
    )
    (result,) = SqlEtfSearchRepository(session).search(_criteria()).results

    assert (result.ticker, result.name, result.exchange) == (
        "SPY",
        "SPDR S&P 500 ETF Trust",
        "NYSE",
    )
    assert (result.net_assets, result.expense_ratio, result.category) == (
        5e11,
        0.09,
        "large_blend",
    )


def test_categories_are_distinct_sorted_and_null_free(session):
    _seed(session, "SPY", category="large_blend")
    _seed(session, "IVV", category="large_blend")
    _seed(session, "QQQ", category="large_growth")
    _seed(session, "GLD", category="commodities_focused")
    _seed(session, "NEW", category=None)  # uncategorised — contributes nothing
    r = SqlEtfSearchRepository(session)

    assert r.categories().categories == (
        "commodities_focused",
        "large_blend",
        "large_growth",
    )
