"""Tests for the database-backed universe repositories.

Offline: an in-memory SQLite database stands in for the real ``stocks`` table (the universe
has no table of its own). Two suites:

- ``SqlUniverseRepository`` (write side): the additive upsert (insert new / refresh in place /
  never remove an absent member), the fill-but-don't-clobber rule for the anchor's
  name/exchange/sector, the screen stamp, added-vs-updated counting, and the enrichment pass's
  read/write of the sector/industry classification.
- ``SqlStockSearchRepository`` (read side): the name-or-ticker substring match, the
  sector/industry/index-membership filters, the sorts (market cap + trailing growth, nulls
  last, stable ticker tiebreak), limit/offset paging with a total count, the screened-only
  gate, and the distinct sector/industry menus.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.stocks.models import StockRecord, get_or_create_stock
from app.stocks.universe.db_repository import (
    SqlStockSearchRepository,
    SqlUniverseRepository,
)
from app.stocks.universe.entities import (
    CompanyClassification,
    ScreenedStock,
    SortDirection,
    StockSearchCriteria,
    StockSort,
)

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


def test_tickers_missing_classification_lists_unclassified_by_market_cap_and_capped(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", market_cap=3e12),
            _stock("MSFT", market_cap=2e12),
            _stock("XOM", market_cap=5e11),
        )
    )
    # A non-screened, incidentally-known ticker counts too — the work-list spans the whole
    # stocks table, not only screened members — but with no market cap it sorts last.
    get_or_create_stock(session, "TSLA", None)
    session.commit()
    # Fully classify one so it drops out of the work-list.
    r.set_classification(
        "MSFT", CompanyClassification(sector="technology", industry="software_infrastructure")
    )

    # Largest market cap first (the megacaps before the tail), the null-cap incidental
    # ticker last, and capped to the limit — so a run classifies the biggest names first.
    assert r.tickers_missing_classification(10) == ("AAPL", "XOM", "TSLA")
    assert r.tickers_missing_classification(2) == ("AAPL", "XOM")


def test_tickers_missing_classification_includes_a_one_sided_classification(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))
    # Yahoo gave only the industry last run — the sector is still null, so the stock must
    # remain on the work-list until both sides are filled (not stuck half-classified).
    r.set_classification("AAPL", CompanyClassification(industry="consumer_electronics"))

    assert r.tickers_missing_classification(10) == ("AAPL",)


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


# --- SqlStockSearchRepository (the read side) ----------------------------------------------


def _seed(
    session,
    ticker,
    *,
    name=None,
    sector=None,
    industry=None,
    market_cap=1e10,
    revenue_growth_yoy=None,
    eps_growth_yoy=None,
    in_sp500=False,
    in_nasdaq100=False,
):
    """Insert a ``stocks`` anchor row directly — the search reads whatever the sync/annual
    slices would have written (a ``market_cap`` marks the row as screened; ``None`` leaves it an
    incidental, non-searchable ticker)."""
    session.add(
        StockRecord(
            ticker=ticker,
            name=name,
            sector=sector,
            industry=industry,
            market_cap=market_cap,
            revenue_growth_yoy=revenue_growth_yoy,
            eps_growth_yoy=eps_growth_yoy,
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
        )
    )
    session.commit()


def _criteria(**overrides) -> StockSearchCriteria:
    base = dict(
        query=None,
        sector=None,
        industry=None,
        in_sp500=None,
        in_nasdaq100=None,
        sort=StockSort.MARKET_CAP,
        direction=SortDirection.DESC,
        limit=50,
        offset=0,
    )
    base.update(overrides)
    return StockSearchCriteria(**base)


def _tickers(page) -> list[str]:
    return [r.ticker for r in page.results]


def test_search_matches_name_or_ticker_substring_case_insensitively(session):
    _seed(session, "NVDA", name="Nvidia")
    _seed(session, "NVAX", name="Novavax")  # matches by ticker only (name has no "nv")
    _seed(session, "AAPL", name="Apple Inc.")
    r = SqlStockSearchRepository(session)

    # The headline example: typing "NV" surfaces Nvidia (by name) and NVAX (by ticker).
    assert set(_tickers(r.search(_criteria(query="NV")))) == {"NVDA", "NVAX"}
    # Case-insensitive — lower-case query, same hits.
    assert set(_tickers(r.search(_criteria(query="nv")))) == {"NVDA", "NVAX"}
    # A name-only fragment matches just the one company.
    assert _tickers(r.search(_criteria(query="nvid"))) == ["NVDA"]


def test_search_treats_like_metacharacters_literally(session):
    _seed(session, "NVDA", name="Nvidia")
    r = SqlStockSearchRepository(session)
    # "%" is escaped, so it matches a literal percent (none here) rather than "everything".
    assert _tickers(r.search(_criteria(query="%"))) == []


def test_search_filters_by_sector_and_industry(session):
    _seed(session, "NVDA", sector="technology", industry="semiconductors")
    _seed(session, "MSFT", sector="technology", industry="software_infrastructure")
    _seed(session, "XOM", sector="energy", industry="oil_gas_integrated")
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(sector="technology")))) == {"NVDA", "MSFT"}
    assert _tickers(r.search(_criteria(industry="semiconductors"))) == ["NVDA"]
    # Both filters AND together.
    assert _tickers(
        r.search(_criteria(sector="technology", industry="software_infrastructure"))
    ) == ["MSFT"]


def test_search_filters_by_index_membership(session):
    _seed(session, "AAPL", in_sp500=True, in_nasdaq100=True)
    _seed(session, "XOM", in_sp500=True, in_nasdaq100=False)
    _seed(session, "ASML", in_sp500=False, in_nasdaq100=True)
    _seed(session, "SMCI", in_sp500=False, in_nasdaq100=False)
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(in_sp500=True)))) == {"AAPL", "XOM"}
    assert set(_tickers(r.search(_criteria(in_nasdaq100=True)))) == {"AAPL", "ASML"}
    assert _tickers(r.search(_criteria(in_sp500=False, in_nasdaq100=False))) == ["SMCI"]
    # A tri-state None doesn't filter — everyone is returned.
    assert len(r.search(_criteria()).results) == 4


def test_search_sorts_by_market_cap_both_directions(session):
    _seed(session, "MEGA", market_cap=3e12)
    _seed(session, "BIG", market_cap=1e12)
    _seed(session, "MID", market_cap=5e11)
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=StockSort.MARKET_CAP))) == ["MEGA", "BIG", "MID"]
    assert _tickers(
        r.search(_criteria(sort=StockSort.MARKET_CAP, direction=SortDirection.ASC))
    ) == ["MID", "BIG", "MEGA"]


def test_search_sorts_by_growth_with_nulls_last_either_direction(session):
    _seed(session, "AAA", revenue_growth_yoy=10.0)
    _seed(session, "BBB", revenue_growth_yoy=30.0)
    _seed(session, "CCC", revenue_growth_yoy=None)  # unfilled growth sinks to the bottom
    _seed(session, "DDD", revenue_growth_yoy=20.0)
    r = SqlStockSearchRepository(session)

    # Descending: 30, 20, 10, then the null.
    assert _tickers(r.search(_criteria(sort=StockSort.REVENUE_GROWTH))) == [
        "BBB",
        "DDD",
        "AAA",
        "CCC",
    ]
    # Ascending: 10, 20, 30, and the null is STILL last (nulls_last, not just reversed).
    assert _tickers(
        r.search(_criteria(sort=StockSort.REVENUE_GROWTH, direction=SortDirection.ASC))
    ) == ["AAA", "DDD", "BBB", "CCC"]


def test_search_breaks_sort_ties_by_ticker_for_stable_paging(session):
    _seed(session, "TWOB", market_cap=1e12)
    _seed(session, "TWOA", market_cap=1e12)  # same cap — ticker decides the order
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=StockSort.MARKET_CAP))) == ["TWOA", "TWOB"]


def test_search_paginates_with_a_total_count(session):
    for i, cap in enumerate([5e12, 4e12, 3e12, 2e12, 1e12]):
        _seed(session, f"E{5 - i}", market_cap=cap)  # E5..E1, biggest first
    r = SqlStockSearchRepository(session)

    first = r.search(_criteria(limit=2, offset=0))
    assert (_tickers(first), first.total, first.limit, first.offset) == (
        ["E5", "E4"],
        5,
        2,
        0,
    )
    assert _tickers(r.search(_criteria(limit=2, offset=2))) == ["E3", "E2"]
    last = r.search(_criteria(limit=2, offset=4))
    assert (_tickers(last), last.total) == (["E1"], 5)  # total is the full match count


def test_search_excludes_unscreened_incidental_rows(session):
    _seed(session, "NVDA", name="Nvidia", market_cap=3e12)  # screened
    # An incidentally-known ticker (a card lookup): a row with no market cap.
    get_or_create_stock(session, "INCID", "Incidental Co")
    session.commit()
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria())) == ["NVDA"]  # the unscreened row is invisible
    # Even a name that would match is filtered out by the screened gate.
    assert r.search(_criteria(query="incidental")).results == ()


def test_search_maps_every_row_field(session):
    _seed(
        session,
        "NVDA",
        name="Nvidia",
        sector="technology",
        industry="semiconductors",
        market_cap=3.0e12,
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
        in_sp500=True,
        in_nasdaq100=True,
    )
    (result,) = SqlStockSearchRepository(session).search(_criteria()).results

    assert (result.ticker, result.name, result.sector, result.industry) == (
        "NVDA",
        "Nvidia",
        "technology",
        "semiconductors",
    )
    assert result.market_cap == 3.0e12
    assert (result.revenue_growth_yoy, result.eps_growth_yoy) == (61.6, 587.4)
    assert (result.in_sp500, result.in_nasdaq100) == (True, True)


def test_classifications_are_distinct_sorted_and_null_free(session):
    _seed(session, "NVDA", sector="technology", industry="semiconductors")
    _seed(session, "MSFT", sector="technology", industry="software_infrastructure")
    _seed(session, "XOM", sector="energy", industry="oil_gas_integrated")
    _seed(session, "QQQ", sector=None, industry=None)  # unclassified — contributes nothing
    r = SqlStockSearchRepository(session)

    result = r.classifications()

    assert result.sectors == ("energy", "technology")  # distinct + sorted, no null
    assert result.industries == (
        "oil_gas_integrated",
        "semiconductors",
        "software_infrastructure",
    )
