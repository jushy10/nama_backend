"""Tests for the database-backed ETF repositories.

Offline: an in-memory SQLite database stands in for the real ``etfs`` table. Two suites:

- ``SqlEtfRepository`` (write side): the additive upsert (insert new / refresh in place / never
  remove an absent fund), the fill-but-don't-clobber rule for name/exchange, the screen stamp,
  and added-vs-updated counting.
- ``SqlEtfSearchRepository`` (read side): the name-or-ticker substring match, the sorts (net
  assets / YTD return / expense ratio, nulls last, stable ticker tiebreak), and limit/offset
  paging with a total count.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.etfs.db_repository import SqlEtfRepository, SqlEtfSearchRepository
from app.stocks.etfs.entities import (
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


def _etf(ticker, *, name=None, exchange=None, net_assets=1e10, expense_ratio=0.2, ytd_return=5.0):
    return ScreenedEtf(
        ticker=ticker,
        name=name,
        exchange=exchange,
        net_assets=net_assets,
        expense_ratio=expense_ratio,
        ytd_return=ytd_return,
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
                exchange="NYSEARCA",
                net_assets=5e11,
                expense_ratio=0.09,
                ytd_return=6.5,
            ),
            _etf("QQQ", name="Invesco QQQ Trust", net_assets=3e11),
        )
    )

    assert (counts.added, counts.updated) == (2, 0)
    assert _count(session) == 2
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange, spy.net_assets, spy.expense_ratio, spy.ytd_return) == (
        "SPDR S&P 500 ETF Trust",
        "NYSEARCA",
        5e11,
        0.09,
        6.5,
    )
    # The screen time is stamped on the row (SQLite hands it back naive).
    assert spy.screened_at.replace(tzinfo=timezone.utc) == _NOW
    qqq = _row(session, "QQQ")
    assert (qqq.name, qqq.exchange) == ("Invesco QQQ Trust", None)


def test_upsert_refreshes_figures_in_place(session):
    r = repo(session)
    r.upsert_screen((_etf("SPY", net_assets=5.0e11, ytd_return=3.0),))
    counts = r.upsert_screen((_etf("SPY", net_assets=5.4e11, ytd_return=7.2),))

    assert (counts.added, counts.updated) == (0, 1)
    assert _count(session) == 1  # refreshed, not duplicated
    spy = _row(session, "SPY")
    assert (spy.net_assets, spy.ytd_return) == (5.4e11, 7.2)


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
    # First screen knows the name but not the exchange.
    r.upsert_screen((_etf("SPY", name="SPDR S&P 500", exchange=None),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", None)

    # A later, nameless screen learns the exchange: the name survives, the exchange fills.
    r.upsert_screen((_etf("SPY", name=None, exchange="NYSEARCA"),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", "NYSEARCA")

    # A different name/exchange never overwrites the settled ones.
    r.upsert_screen((_etf("SPY", name="Something Else", exchange="NYSE"),))
    spy = _row(session, "SPY")
    assert (spy.name, spy.exchange) == ("SPDR S&P 500", "NYSEARCA")


def test_upsert_counts_a_preexisting_unscreened_row_as_added(session):
    # A fund row that exists but was never screened (screened_at still null).
    get_or_create_etf(session, "SPY", "SPDR S&P 500")
    session.commit()

    counts = repo(session).upsert_screen((_etf("SPY", net_assets=5e11),))
    # First time it's screened => added, not updated.
    assert (counts.added, counts.updated) == (1, 0)
    assert _row(session, "SPY").net_assets == 5e11


# --- SqlEtfSearchRepository (the read side) ------------------------------------------------


def _seed(
    session,
    ticker,
    *,
    name=None,
    exchange=None,
    net_assets=1e10,
    expense_ratio=None,
    ytd_return=None,
):
    """Insert an ``etfs`` row directly — whatever the sync would have written."""
    session.add(
        EtfRecord(
            ticker=ticker,
            name=name,
            exchange=exchange,
            net_assets=net_assets,
            expense_ratio=expense_ratio,
            ytd_return=ytd_return,
            screened_at=_NOW,
        )
    )
    session.commit()


def _criteria(**overrides) -> EtfSearchCriteria:
    base = dict(
        query=None,
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
    # Case-insensitive.
    assert set(_tickers(r.search(_criteria(query="GOLD")))) == {"GLD", "RING"}
    # Ticker-only match.
    assert _tickers(r.search(_criteria(query="spy"))) == ["SPY"]


def test_search_treats_like_metacharacters_literally(session):
    _seed(session, "SPY", name="SPDR S&P 500")
    r = SqlEtfSearchRepository(session)
    # "%" is escaped, so it matches a literal percent (none here) rather than "everything".
    assert _tickers(r.search(_criteria(query="%"))) == []


def test_search_sorts_by_net_assets_both_directions(session):
    _seed(session, "BIG", net_assets=5e11)
    _seed(session, "MID", net_assets=1e11)
    _seed(session, "SMALL", net_assets=5e9)
    r = SqlEtfSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=EtfSort.NET_ASSETS))) == ["BIG", "MID", "SMALL"]
    assert _tickers(
        r.search(_criteria(sort=EtfSort.NET_ASSETS, direction=SortDirection.ASC))
    ) == ["SMALL", "MID", "BIG"]


def test_search_sorts_by_expense_ratio_cheapest_first(session):
    _seed(session, "CHEAP", expense_ratio=0.03)
    _seed(session, "MID", expense_ratio=0.2)
    _seed(session, "PRICEY", expense_ratio=0.75)
    r = SqlEtfSearchRepository(session)

    # order=asc surfaces the cheapest funds first — the natural way to browse expense ratio.
    assert _tickers(
        r.search(_criteria(sort=EtfSort.EXPENSE_RATIO, direction=SortDirection.ASC))
    ) == ["CHEAP", "MID", "PRICEY"]


def test_search_sorts_with_nulls_last_either_direction(session):
    _seed(session, "AAA", ytd_return=10.0)
    _seed(session, "BBB", ytd_return=30.0)
    _seed(session, "CCC", ytd_return=None)  # unfilled figure sinks to the bottom
    _seed(session, "DDD", ytd_return=20.0)
    r = SqlEtfSearchRepository(session)

    # Descending: 30, 20, 10, then the null.
    assert _tickers(r.search(_criteria(sort=EtfSort.YTD_RETURN))) == [
        "BBB",
        "DDD",
        "AAA",
        "CCC",
    ]
    # Ascending: 10, 20, 30, and the null is STILL last (nulls_last, not just reversed).
    assert _tickers(
        r.search(_criteria(sort=EtfSort.YTD_RETURN, direction=SortDirection.ASC))
    ) == ["AAA", "DDD", "BBB", "CCC"]


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
        exchange="NYSEARCA",
        net_assets=5e11,
        expense_ratio=0.09,
        ytd_return=6.5,
    )
    (result,) = SqlEtfSearchRepository(session).search(_criteria()).results

    assert (result.ticker, result.name, result.exchange) == (
        "SPY",
        "SPDR S&P 500 ETF Trust",
        "NYSEARCA",
    )
    assert (result.net_assets, result.expense_ratio, result.ytd_return) == (5e11, 0.09, 6.5)
