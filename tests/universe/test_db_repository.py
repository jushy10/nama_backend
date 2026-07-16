"""Tests for the database-backed universe repositories.

Offline: an in-memory SQLite database stands in for the real ``stocks`` table (the universe
has no table of its own). Two suites:

- ``SqlUniverseRepository`` (write side): the additive upsert (insert new / refresh in place /
  never remove an absent member), the fill-but-don't-clobber rule for the anchor's
  name/exchange/sector, the screen stamp, added-vs-updated counting, the enrichment pass's
  read/write of the sector/industry classification, and the valuation pass's overwriting
  ``set_pe_ratios``.
- ``SqlStockSearchRepository`` (read side): the name-or-ticker substring match, the
  sector/industry/index-membership filters, the sorts (market cap, trailing growth, and
  trailing P/E — nulls last, stable ticker tiebreak), limit/offset paging with a total count,
  the screened-only gate, and the distinct sector/industry menus.
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
    AnchorMetrics,
    CompanyClassification,
    MarketCapTier,
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


def _stock(
    ticker,
    *,
    name=None,
    exchange=None,
    market_cap=1e10,
    sector=None,
    country=None,
    currency=None,
    has_us_listing=False,
):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
        country=country,
        currency=currency,
        has_us_listing=has_us_listing,
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


def test_upsert_writes_country_and_currency(session):
    # A Canadian screen row lands its market facts onto the anchor, whole CAD market cap.
    repo(session).upsert_screen(
        (
            _stock("SHOP.TO", name="Shopify", market_cap=1.2e11, country="CA", currency="CAD"),
        )
    )
    row = _row(session, "SHOP.TO")
    assert (row.country, row.currency, row.market_cap) == ("CA", "CAD", 1.2e11)


def test_upsert_writes_and_overwrites_has_us_listing(session):
    # The flag is recomputed each run (not fill-once): a listing flagged as a US duplicate can be
    # un-flagged on a later run when its US sibling is gone.
    r = repo(session)
    r.upsert_screen((_stock("SHOP.TO", market_cap=1.2e11, has_us_listing=True),))
    assert _row(session, "SHOP.TO").has_us_listing is True
    r.upsert_screen((_stock("SHOP.TO", market_cap=1.3e11, has_us_listing=False),))
    assert _row(session, "SHOP.TO").has_us_listing is False  # overwritten, not fill-once


def test_upsert_fills_country_currency_once_then_never_clobbers(session):
    # country/currency are fill-once market facts like the exchange: a later screen that came
    # back without them (or with a different value) never overwrites the settled ones.
    r = repo(session)
    r.upsert_screen((_stock("SHOP.TO", market_cap=1.2e11, country="CA", currency="CAD"),))
    r.upsert_screen((_stock("SHOP.TO", market_cap=1.3e11),))  # no market facts this run
    row = _row(session, "SHOP.TO")
    assert (row.country, row.currency, row.market_cap) == ("CA", "CAD", 1.3e11)


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
    # Fully classify one (sector + industry + domicile) so it drops out of the work-list.
    r.set_classification(
        "MSFT",
        CompanyClassification(
            sector="technology",
            industry="software_infrastructure",
            domicile_country="US",
        ),
    )

    # Largest market cap first (the megacaps before the tail), the null-cap incidental
    # ticker last, and capped to the limit — so a run classifies the biggest names first.
    assert r.tickers_missing_classification(10) == ("AAPL", "XOM", "TSLA")
    assert r.tickers_missing_classification(2) == ("AAPL", "XOM")


def test_tickers_missing_classification_includes_a_classified_row_missing_domicile(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))
    # Sector + industry filled on an earlier run, but domicile was never captured — the stock
    # must stay on the work-list so the enrichment pass revisits it to backfill the domicile.
    r.set_classification(
        "AAPL", CompanyClassification(sector="technology", industry="consumer_electronics")
    )

    assert r.tickers_missing_classification(10) == ("AAPL",)

    # Once the domicile lands too, it finally drops out.
    r.set_classification("AAPL", CompanyClassification(domicile_country="US"))
    assert r.tickers_missing_classification(10) == ()


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


def test_set_classification_fills_domicile_once(session):
    r = repo(session)
    r.upsert_screen((_stock("SHOP.TO", market_cap=1.2e11, country="CA", currency="CAD"),))

    # The domicile rides the same call as sector/industry and is fill-once like them.
    r.set_classification(
        "SHOP.TO",
        CompanyClassification(
            sector="technology", industry="software", domicile_country="CA"
        ),
    )
    assert _row(session, "SHOP.TO").domicile_country == "CA"

    # A later run never clobbers a settled domicile.
    r.set_classification("SHOP.TO", CompanyClassification(domicile_country="US"))
    assert _row(session, "SHOP.TO").domicile_country == "CA"


def test_us_domiciled_company_names_returns_us_listed_us_domiciled_only(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", name="Apple Inc.", market_cap=3e12, country="US", currency="USD"),
            # US-listed but CA-domiciled (Shopify) — must NOT be in the index, so SHOP.TO survives.
            _stock("SHOP", name="Shopify Inc.", market_cap=1e11, country="US", currency="USD"),
            _stock("NONM", name=None, market_cap=1e10, country="US", currency="USD"),
            _stock("RY.TO", name="Royal Bank of Canada", market_cap=4e11, country="CA", currency="CAD"),
        )
    )
    r.set_classification("AAPL", CompanyClassification(domicile_country="US"))
    r.set_classification("SHOP", CompanyClassification(domicile_country="CA"))

    # Only the US-listed, US-domiciled, named row.
    assert r.us_domiciled_company_names() == frozenset({"Apple Inc."})


def test_delete_stocks_removes_rows_and_no_ops_on_empty(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL.TO", market_cap=3e12, country="CA", currency="CAD"),
            _stock("SHOP.TO", market_cap=1e11, country="CA", currency="CAD"),
        )
    )

    assert r.delete_stocks([]) == 0  # no-op
    assert r.delete_stocks(["AAPL.TO", "ZZZZ"]) == 1  # only the existing one is removed
    remaining = {row.ticker for row in session.execute(select(StockRecord)).scalars()}
    assert "AAPL.TO" not in remaining
    assert "SHOP.TO" in remaining


def test_set_classification_ignores_an_unknown_ticker(session):
    # No row for NOPE — a no-op: no row is created and nothing raises.
    repo(session).set_classification("NOPE", CompanyClassification(industry="x"))
    assert (
        session.execute(
            select(StockRecord).where(StockRecord.ticker == "NOPE")
        ).scalar_one_or_none()
        is None
    )


def test_set_pe_ratios_writes_overwrites_and_returns_the_non_null_count(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", market_cap=3e12),
            _stock("MSFT", market_cap=2e12),
            _stock("LOSS", market_cap=1e10),
        )
    )

    written = r.set_pe_ratios({"AAPL": 30.5, "MSFT": 42.0, "LOSS": None})

    assert written == 2  # AAPL + MSFT; the None (a trailing loss) isn't counted
    assert _row(session, "AAPL").pe_ratio == 30.5
    assert _row(session, "MSFT").pe_ratio == 42.0
    assert _row(session, "LOSS").pe_ratio is None

    # Overwrite, not fill-once: a later sweep replaces the figure in place, and a None clears a
    # prior one (the stock's trailing year turned a loss, or its quarters aged out).
    written = r.set_pe_ratios({"AAPL": 28.0, "MSFT": None})
    assert written == 1
    assert _row(session, "AAPL").pe_ratio == 28.0
    assert _row(session, "MSFT").pe_ratio is None  # cleared


def test_fcf_per_share_by_ticker_returns_only_non_null_rows(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12), _stock("MSFT", market_cap=2e12)))
    # The annual slice writes fcf_per_share onto the anchor; here we set it directly.
    _row(session, "AAPL").fcf_per_share = 4.0
    session.commit()

    by_ticker = r.fcf_per_share_by_ticker()

    assert by_ticker == {"AAPL": 4.0}  # MSFT has none -> absent, not a null entry


def test_set_fcf_yields_writes_overwrites_keeps_sign_and_counts(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", market_cap=3e12),
            _stock("BURN", market_cap=1e10),
            _stock("MSFT", market_cap=2e12),
        )
    )

    written = r.set_fcf_yields({"AAPL": 4.0, "BURN": -20.0, "MSFT": None})

    assert written == 2  # AAPL + BURN; the None isn't counted
    assert _row(session, "AAPL").fcf_yield == 4.0
    assert _row(session, "BURN").fcf_yield == -20.0  # sign kept (a cash-burner)
    assert _row(session, "MSFT").fcf_yield is None

    # Overwrite in place, and a None clears a prior figure.
    written = r.set_fcf_yields({"AAPL": 3.5, "BURN": None})
    assert written == 1
    assert _row(session, "AAPL").fcf_yield == 3.5
    assert _row(session, "BURN").fcf_yield is None  # cleared


def test_ev_components_by_ticker_returns_only_rows_with_ebitda(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12), _stock("MSFT", market_cap=2e12)))
    # The fundamentals slice writes the EV components onto the anchor; set them directly here.
    aapl = _row(session, "AAPL")
    aapl.ebitda, aapl.total_debt, aapl.cash_and_equivalents = 5e11, 2e11, 1e11
    # MSFT gets debt/cash but NO ebitda -> excluded (ebitda is the gate).
    _row(session, "MSFT").total_debt = 9e10
    session.commit()

    by_ticker = r.ev_components_by_ticker()

    assert by_ticker == {"AAPL": (5e11, 2e11, 1e11)}  # MSFT absent (no ebitda)


def test_ev_components_by_ticker_carries_null_debt_and_cash(session):
    r = repo(session)
    r.upsert_screen((_stock("DEBTFREE", market_cap=1e12),))
    _row(session, "DEBTFREE").ebitda = 5e11  # only ebitda, no debt/cash
    session.commit()

    assert r.ev_components_by_ticker() == {"DEBTFREE": (5e11, None, None)}


def test_set_ev_ebitda_writes_overwrites_keeps_sign_and_counts(session):
    r = repo(session)
    r.upsert_screen(
        (
            _stock("AAPL", market_cap=3e12),
            _stock("CASHPILE", market_cap=1e10),
            _stock("MSFT", market_cap=2e12),
        )
    )

    written = r.set_ev_ebitda({"AAPL": 6.2, "CASHPILE": -40.0, "MSFT": None})

    assert written == 2  # AAPL + CASHPILE; the None isn't counted
    assert _row(session, "AAPL").ev_to_ebitda == 6.2
    assert _row(session, "CASHPILE").ev_to_ebitda == -40.0  # sign kept (net-cash name)
    assert _row(session, "MSFT").ev_to_ebitda is None

    # Overwrite in place, and a None clears a prior figure.
    written = r.set_ev_ebitda({"AAPL": 5.5, "CASHPILE": None})
    assert written == 1
    assert _row(session, "AAPL").ev_to_ebitda == 5.5
    assert _row(session, "CASHPILE").ev_to_ebitda is None  # cleared


def test_set_ev_ebitda_skips_an_unknown_ticker(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))
    # An unknown ticker is silently skipped (no row to write), so only AAPL counts.
    assert r.set_ev_ebitda({"AAPL": 6.2, "GHOST": 10.0}) == 1


def test_set_pe_ratios_skips_an_unknown_ticker(session):
    r = repo(session)
    r.upsert_screen((_stock("AAPL", market_cap=3e12),))

    # NOPE has no anchor row — it's skipped (not created); AAPL is still written.
    written = r.set_pe_ratios({"AAPL": 20.0, "NOPE": 15.0})

    assert written == 1
    assert _row(session, "AAPL").pe_ratio == 20.0
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
    pe_ratio=None,
    fcf_yield=None,
    ev_ebitda=None,
    fcf_per_share=None,
    revenue_growth_yoy=None,
    eps_growth_yoy=None,
    fcf_growth_yoy=None,
    forward_revenue_growth_yoy=None,
    forward_eps_growth_yoy=None,
    in_sp500=False,
    in_nasdaq100=False,
    country=None,
    currency=None,
    domicile_country=None,
    has_us_listing=False,
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
            pe_ratio=pe_ratio,
            fcf_yield=fcf_yield,
            ev_to_ebitda=ev_ebitda,
            fcf_per_share=fcf_per_share,
            revenue_growth_yoy=revenue_growth_yoy,
            eps_growth_yoy=eps_growth_yoy,
            fcf_growth_yoy=fcf_growth_yoy,
            forward_revenue_growth_yoy=forward_revenue_growth_yoy,
            forward_eps_growth_yoy=forward_eps_growth_yoy,
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            country=country,
            currency=currency,
            domicile_country=domicile_country,
            has_us_listing=has_us_listing,
        )
    )
    session.commit()


def _criteria(**overrides) -> StockSearchCriteria:
    base = dict(
        query=None,
        sectors=(),
        industries=(),
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

    assert set(_tickers(r.search(_criteria(sectors=("technology",))))) == {"NVDA", "MSFT"}
    assert _tickers(r.search(_criteria(industries=("semiconductors",)))) == ["NVDA"]
    # Sector and industry AND together (across axes).
    assert _tickers(
        r.search(_criteria(sectors=("technology",), industries=("software_infrastructure",)))
    ) == ["MSFT"]


def test_search_matches_any_of_several_sectors_or_industries(session):
    _seed(session, "NVDA", sector="technology", industry="semiconductors")
    _seed(session, "MSFT", sector="technology", industry="software_infrastructure")
    _seed(session, "XOM", sector="energy", industry="oil_gas_integrated")
    _seed(session, "JPM", sector="financials", industry="banks_diversified")
    r = SqlStockSearchRepository(session)

    # Several sectors OR within the axis — the union of technology and energy.
    assert set(_tickers(r.search(_criteria(sectors=("technology", "energy"))))) == {
        "NVDA",
        "MSFT",
        "XOM",
    }
    # Several industries likewise.
    assert set(
        _tickers(r.search(_criteria(industries=("semiconductors", "oil_gas_integrated"))))
    ) == {"NVDA", "XOM"}
    # The two axes still AND: (technology OR energy) AND (semiconductors OR banks) == NVDA.
    assert _tickers(
        r.search(
            _criteria(
                sectors=("technology", "energy"),
                industries=("semiconductors", "banks_diversified"),
            )
        )
    ) == ["NVDA"]


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


def test_search_filters_by_country(session):
    _seed(session, "AAPL", country="US", currency="USD")
    _seed(session, "XOM", country="US", currency="USD")
    _seed(session, "SHOP.TO", country="CA", currency="CAD")
    _seed(session, "RY.TO", country="CA", currency="CAD")
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(countries=("CA",))))) == {"SHOP.TO", "RY.TO"}
    assert set(_tickers(r.search(_criteria(countries=("US",))))) == {"AAPL", "XOM"}
    # A union of markets returns both; an empty tuple doesn't filter (every market).
    assert len(r.search(_criteria(countries=("US", "CA"))).results) == 4
    assert len(r.search(_criteria()).results) == 4


def test_us_screen_excludes_canadian_domiciled_listings(session):
    # The US screen (single market, default) scopes to US *home* companies by issuer domicile:
    # a Canadian company's US listing (CNI, domicile CA) drops out, while US companies, foreign
    # ADRs (TSM, domicile TW), and rows whose domicile isn't known yet all stay.
    _seed(session, "AAPL", country="US", currency="USD", domicile_country="US")
    _seed(session, "CNI", country="US", currency="USD", domicile_country="CA")
    _seed(session, "TSM", country="US", currency="USD", domicile_country="TW")
    _seed(session, "UNKN", country="US", currency="USD", domicile_country=None)
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(countries=("US",))))) == {"AAPL", "TSM", "UNKN"}


def test_ca_screen_excludes_foreign_domiciled_cdrs(session):
    # The Canadian screen (single market, default) scopes to Canadian *home* companies: the CDRs
    # of US / foreign issuers (ZCVX.NE domicile US, NEST.NE domicile CH) drop out, while Canadian
    # companies — including one dual-listed in the US (CNR.TO) — and not-yet-known rows stay.
    _seed(session, "SHOP.TO", country="CA", currency="CAD", domicile_country="CA")
    _seed(session, "CNR.TO", country="CA", currency="CAD", domicile_country="CA")
    _seed(session, "ZCVX.NE", country="CA", currency="CAD", domicile_country="US")
    _seed(session, "NEST.NE", country="CA", currency="CAD", domicile_country="CH")
    _seed(session, "UNK.TO", country="CA", currency="CAD", domicile_country=None)
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(countries=("CA",))))) == {
        "SHOP.TO",
        "CNR.TO",
        "UNK.TO",
    }


def test_ca_screen_excludes_every_cboe_canada_ne_listing(session):
    # The structural CDR guard: EVERY Cboe Canada (.NE) listing is dropped from the CA screen,
    # unconditionally -- regardless of its domicile. It must be unconditional because Yahoo reports
    # some CDRs' country as *Canada* (the receipt's listing country), so a "unless domicile is CA"
    # carve-out would let those CA-mislabeled CDRs (CHEV.NE here) back in. TSX (.TO) / TSXV (.V)
    # listings are unaffected -- kept unless their domicile is a confirmed foreign country.
    _seed(session, "ZAAP.NE", country="CA", currency="CAD", domicile_country=None)  # CDR, not yet backfilled
    _seed(session, "CHEV.NE", country="CA", currency="CAD", domicile_country="CA")  # CDR Yahoo mislabels as Canada
    _seed(session, "INTC.NE", country="CA", currency="CAD", domicile_country="US")  # CDR labelled US
    _seed(session, "SHOP.TO", country="CA", currency="CAD", domicile_country=None)  # genuine TSX, unknown domicile
    _seed(session, "STONE.TO", country="CA", currency="CAD", domicile_country=None)  # ".NE" is a suffix — "STONE" isn't a CDR
    r = SqlStockSearchRepository(session)

    # Every .NE listing drops (whatever its domicile); only the TSX names remain.
    assert set(_tickers(r.search(_criteria(countries=("CA",))))) == {
        "SHOP.TO",
        "STONE.TO",
    }
    # Opting into the duplicates brings the .NE CDRs back.
    assert set(
        _tickers(r.search(_criteria(countries=("CA",), include_interlisted=True)))
    ) == {"ZAAP.NE", "CHEV.NE", "INTC.NE", "SHOP.TO", "STONE.TO"}


def test_include_interlisted_skips_domicile_scoping(session):
    # Opting in shows every listing in the market, cross-listed duplicates included.
    _seed(session, "SHOP.TO", country="CA", currency="CAD", domicile_country="CA")
    _seed(session, "ZCVX.NE", country="CA", currency="CAD", domicile_country="US")
    r = SqlStockSearchRepository(session)

    assert set(
        _tickers(r.search(_criteria(countries=("CA",), include_interlisted=True)))
    ) == {"SHOP.TO", "ZCVX.NE"}


def test_domicile_scoping_only_applies_to_a_single_market(session):
    # The home-market scoping is a single-market concept: a multi-market query (or no market)
    # returns every listing, foreign-domiciled duplicates included.
    _seed(session, "CNI", country="US", currency="USD", domicile_country="CA")
    _seed(session, "ZCVX.NE", country="CA", currency="CAD", domicile_country="US")
    _seed(session, "AAPL", country="US", currency="USD", domicile_country="US")
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(countries=("US", "CA"))))) == {
        "CNI",
        "ZCVX.NE",
        "AAPL",
    }
    assert set(_tickers(r.search(_criteria()))) == {"CNI", "ZCVX.NE", "AAPL"}


def test_search_maps_country_and_currency_onto_the_result(session):
    _seed(session, "SHOP.TO", name="Shopify", country="CA", currency="CAD", market_cap=1.2e11)
    r = SqlStockSearchRepository(session)

    (row,) = r.search(_criteria(countries=("CA",))).results
    assert (row.country, row.currency, row.market_cap) == ("CA", "CAD", 1.2e11)


def test_search_filters_by_market_cap_tier(session):
    # Names straddling every tier seam — the ranges are half-open [low, high), so a stock
    # sitting exactly on a boundary belongs to the *upper* tier.
    _seed(session, "MEGA", market_cap=250e9)
    _seed(session, "AT200B", market_cap=200e9)  # mega floor -> mega, not large
    _seed(session, "LARGE", market_cap=50e9)
    _seed(session, "AT10B", market_cap=10e9)  # large floor -> large, not mid
    _seed(session, "MID", market_cap=5e9)
    _seed(session, "AT2B", market_cap=2e9)  # mid floor -> mid, not small
    _seed(session, "SMALL", market_cap=1.5e9)
    r = SqlStockSearchRepository(session)

    assert set(_tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.MEGA,))))) == {
        "MEGA",
        "AT200B",
    }
    assert set(_tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.LARGE,))))) == {
        "LARGE",
        "AT10B",
    }
    assert set(_tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.MID,))))) == {
        "MID",
        "AT2B",
    }
    assert _tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.SMALL,)))) == ["SMALL"]
    # No tier => every screened size is returned.
    assert len(r.search(_criteria()).results) == 7


def test_search_filters_by_multiple_market_cap_tiers_as_a_union(session):
    _seed(session, "MEGA", market_cap=250e9)
    _seed(session, "LARGE", market_cap=50e9)
    _seed(session, "MID", market_cap=5e9)
    _seed(session, "SMALL", market_cap=1.5e9)
    r = SqlStockSearchRepository(session)

    # Two adjacent tiers merge into one contiguous span ($2B–$200B).
    assert set(
        _tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.LARGE, MarketCapTier.MID))))
    ) == {"LARGE", "MID"}
    # Non-adjacent tiers are a disjoint union — the ends without the middle.
    assert set(
        _tickers(r.search(_criteria(market_cap_tiers=(MarketCapTier.MEGA, MarketCapTier.SMALL))))
    ) == {"MEGA", "SMALL"}


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


def test_search_sorts_by_the_combined_growth_blend_nulls_last(session):
    # GROWTH ranks by the equal-weight average of the two trailing-growth figures.
    _seed(session, "AAA", revenue_growth_yoy=10.0, eps_growth_yoy=10.0)  # blend 10
    _seed(session, "BBB", revenue_growth_yoy=40.0, eps_growth_yoy=20.0)  # blend 30
    _seed(session, "CCC", revenue_growth_yoy=20.0, eps_growth_yoy=20.0)  # blend 20
    # Missing *either* leg makes the blend null (a null + a number is null in SQL), so it sinks
    # to the bottom in either direction — the blend ranks only stocks that have both figures.
    _seed(session, "DDD", revenue_growth_yoy=100.0, eps_growth_yoy=None)
    _seed(session, "EEE", revenue_growth_yoy=None, eps_growth_yoy=None)
    r = SqlStockSearchRepository(session)

    # Descending: 30, 20, 10, then the two blend-null names (ticker tiebreak).
    assert _tickers(r.search(_criteria(sort=StockSort.GROWTH))) == [
        "BBB",
        "CCC",
        "AAA",
        "DDD",
        "EEE",
    ]
    # Ascending: 10, 20, 30, and the nulls are STILL last (nulls_last, not just reversed).
    assert _tickers(
        r.search(_criteria(sort=StockSort.GROWTH, direction=SortDirection.ASC))
    ) == ["AAA", "CCC", "BBB", "DDD", "EEE"]


def test_search_sorts_by_the_forward_growth_figures_nulls_last(session):
    # The forward (FY1->FY2 consensus) single-metric sorts behave exactly like the trailing
    # ones: rank by the figure, nulls last, ticker tiebreak. CCC has no forward pair yet
    # (awaiting a second upcoming year), so it sinks in both.
    _seed(session, "AAA", forward_revenue_growth_yoy=10.0, forward_eps_growth_yoy=15.0)
    _seed(session, "BBB", forward_revenue_growth_yoy=30.0, forward_eps_growth_yoy=40.0)
    _seed(session, "CCC", forward_revenue_growth_yoy=None, forward_eps_growth_yoy=None)
    _seed(session, "DDD", forward_revenue_growth_yoy=20.0, forward_eps_growth_yoy=25.0)
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=StockSort.FORWARD_REVENUE_GROWTH))) == [
        "BBB",
        "DDD",
        "AAA",
        "CCC",
    ]
    assert _tickers(r.search(_criteria(sort=StockSort.FORWARD_EPS_GROWTH))) == [
        "BBB",
        "DDD",
        "AAA",
        "CCC",
    ]


def test_search_sorts_by_the_forward_growth_blend_nulls_last(session):
    # FORWARD_GROWTH blends the two forward figures the same way GROWTH blends the trailing pair:
    # a NULL on either leg makes the sum (and blend) NULL, so the stock sorts last either way.
    _seed(session, "AAA", forward_revenue_growth_yoy=10.0, forward_eps_growth_yoy=10.0)  # 10
    _seed(session, "BBB", forward_revenue_growth_yoy=40.0, forward_eps_growth_yoy=20.0)  # 30
    _seed(session, "CCC", forward_revenue_growth_yoy=20.0, forward_eps_growth_yoy=20.0)  # 20
    _seed(session, "DDD", forward_revenue_growth_yoy=100.0, forward_eps_growth_yoy=None)  # null
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=StockSort.FORWARD_GROWTH))) == [
        "BBB",
        "CCC",
        "AAA",
        "DDD",
    ]
    assert _tickers(
        r.search(_criteria(sort=StockSort.FORWARD_GROWTH, direction=SortDirection.ASC))
    ) == ["AAA", "CCC", "BBB", "DDD"]


def test_search_sorts_by_pe_with_nulls_last_either_direction(session):
    _seed(session, "CHEAP", pe_ratio=12.0)
    _seed(session, "MID", pe_ratio=25.0)
    _seed(session, "RICH", pe_ratio=80.0)
    _seed(session, "NONE", pe_ratio=None)  # unvalued (or a trailing loss) sinks to the bottom
    r = SqlStockSearchRepository(session)

    # Ascending surfaces the cheapest on earnings first; the null is last.
    assert _tickers(
        r.search(_criteria(sort=StockSort.PE, direction=SortDirection.ASC))
    ) == ["CHEAP", "MID", "RICH", "NONE"]
    # Descending: priciest first, and the null is STILL last (nulls_last, not just reversed).
    assert _tickers(r.search(_criteria(sort=StockSort.PE))) == [
        "RICH",
        "MID",
        "CHEAP",
        "NONE",
    ]


def test_search_sorts_by_fcf_yield_with_nulls_last_either_direction(session):
    _seed(session, "RICHCASH", fcf_yield=8.0)  # cheapest on cash (highest yield)
    _seed(session, "MIDCASH", fcf_yield=3.0)
    _seed(session, "BURN", fcf_yield=-5.0)  # negative yield kept — a cash-burner
    _seed(session, "NONE", fcf_yield=None)  # unvalued sinks to the bottom
    r = SqlStockSearchRepository(session)

    # Descending surfaces the cheapest on cash (highest yield) first; the null is last.
    assert _tickers(r.search(_criteria(sort=StockSort.FCF_YIELD))) == [
        "RICHCASH",
        "MIDCASH",
        "BURN",
        "NONE",
    ]
    # Ascending: the cash-burner (negative) first, and the null is STILL last (nulls_last).
    assert _tickers(
        r.search(_criteria(sort=StockSort.FCF_YIELD, direction=SortDirection.ASC))
    ) == ["BURN", "MIDCASH", "RICHCASH", "NONE"]


def test_search_sorts_by_ev_ebitda_with_nulls_last_either_direction(session):
    _seed(session, "CHEAP", ev_ebitda=6.0)  # cheapest on enterprise value
    _seed(session, "MID", ev_ebitda=12.0)
    _seed(session, "NETCASH", ev_ebitda=-4.0)  # negative kept — valued below net cash
    _seed(session, "NONE", ev_ebitda=None)  # unvalued sinks to the bottom
    r = SqlStockSearchRepository(session)

    # Ascending surfaces the cheapest on enterprise value first (the net-cash name lowest of
    # all); the null is last (nulls_last).
    assert _tickers(
        r.search(_criteria(sort=StockSort.EV_EBITDA, direction=SortDirection.ASC))
    ) == ["NETCASH", "CHEAP", "MID", "NONE"]
    # Descending: the priciest first, and the null is STILL last.
    assert _tickers(r.search(_criteria(sort=StockSort.EV_EBITDA))) == [
        "MID",
        "CHEAP",
        "NETCASH",
        "NONE",
    ]


def test_search_with_no_sort_orders_by_ticker_ignoring_direction(session):
    # No sort chosen (sort=None): a neutral A→Z by ticker, independent of market cap — so an
    # unsorted browse isn't secretly market-cap ordered, yet still pages deterministically.
    _seed(session, "CCC", market_cap=3e12)  # biggest, but sorts last by ticker
    _seed(session, "AAA", market_cap=1e12)
    _seed(session, "BBB", market_cap=2e12)
    r = SqlStockSearchRepository(session)

    assert _tickers(r.search(_criteria(sort=None))) == ["AAA", "BBB", "CCC"]
    # Direction doesn't apply without a sort field — still A→Z, not reversed.
    assert _tickers(
        r.search(_criteria(sort=None, direction=SortDirection.ASC))
    ) == ["AAA", "BBB", "CCC"]


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
        pe_ratio=48.2,
        ev_ebitda=42.5,
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
        forward_revenue_growth_yoy=52.1,
        forward_eps_growth_yoy=48.3,
        in_sp500=True,
        in_nasdaq100=True,
        country="US",
        currency="USD",
    )
    (result,) = SqlStockSearchRepository(session).search(_criteria()).results

    assert (result.ticker, result.name, result.sector, result.industry) == (
        "NVDA",
        "Nvidia",
        "technology",
        "semiconductors",
    )
    assert result.market_cap == 3.0e12
    assert result.pe_ratio == 48.2
    assert result.ev_ebitda == 42.5
    assert (result.revenue_growth_yoy, result.eps_growth_yoy) == (61.6, 587.4)
    assert (result.forward_revenue_growth_yoy, result.forward_eps_growth_yoy) == (52.1, 48.3)
    assert (result.in_sp500, result.in_nasdaq100) == (True, True)
    assert (result.country, result.currency) == ("US", "USD")


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


def test_pe_ratios_for_industry_returns_positive_pes_of_that_industry(session):
    _seed(session, "NVDA", industry="semiconductors", pe_ratio=46.5)
    _seed(session, "AMD", industry="semiconductors", pe_ratio=30.0)
    _seed(session, "INTC", industry="semiconductors", pe_ratio=None)  # unvalued — excluded
    _seed(session, "LOSS", industry="semiconductors", pe_ratio=-5.0)  # trailing loss — excluded
    _seed(session, "MSFT", industry="software_infrastructure", pe_ratio=35.0)  # other industry
    r = SqlStockSearchRepository(session)

    pes = r.pe_ratios_for_industry("semiconductors")

    # Only the two positive semis — the null and the negative P/E are filtered out, and the
    # other industry doesn't leak in.
    assert sorted(pes) == [30.0, 46.5]


def test_pe_ratios_for_industry_drops_the_small_cap_tail(session):
    # Mid-cap-and-up only: the benchmark sample excludes the $1–2B slice, whose thin,
    # noisy multiples aren't good comparables — but keeps everything from $2B up.
    _seed(session, "NVDA", industry="semiconductors", market_cap=3e12, pe_ratio=46.5)
    _seed(session, "ATFLOOR", industry="semiconductors", market_cap=2e9, pe_ratio=25.0)  # $2B — kept
    _seed(session, "SMALL", industry="semiconductors", market_cap=1.5e9, pe_ratio=90.0)  # <$2B — dropped
    r = SqlStockSearchRepository(session)

    pes = r.pe_ratios_for_industry("semiconductors")

    assert sorted(pes) == [25.0, 46.5]


def test_pe_ratios_for_industry_empty_for_an_unknown_industry(session):
    _seed(session, "NVDA", industry="semiconductors", pe_ratio=46.5)
    assert SqlStockSearchRepository(session).pe_ratios_for_industry("nonesuch") == ()


def test_industry_for_ticker_returns_the_stored_slug(session):
    _seed(session, "NVDA", industry="semiconductors", pe_ratio=46.5)
    assert (
        SqlStockSearchRepository(session).industry_for_ticker("NVDA")
        == "semiconductors"
    )


def test_industry_for_ticker_none_when_unknown_or_unclassified(session):
    # No row at all, and a row present but with no industry yet, both read as None —
    # the analysis path treats either as "no peer benchmark to attach".
    _seed(session, "NEW", industry=None)
    r = SqlStockSearchRepository(session)
    assert r.industry_for_ticker("ZZZZ") is None  # no such anchor row
    assert r.industry_for_ticker("NEW") is None  # row exists, industry unclassified


def test_anchor_metrics_for_ticker_returns_the_stored_figures(session):
    # The analysis reads every anchor-materialized fundamental off the row in one go — the
    # annual slice's cash/growth, the fundamentals slice's margins/ratios/per-share inputs and
    # enterprise-value inputs, plus the screen's market cap and the clean display name.
    _seed(
        session,
        "NVDA",
        name="Nvidia",
        market_cap=3e12,
        fcf_per_share=9.99,
        revenue_growth_yoy=15.5,
        eps_growth_yoy=22.0,
    )
    # The EV inputs aren't _seed params; set them on the row like the peers test does.
    nvda = _row(session, "NVDA")
    nvda.ebitda, nvda.total_debt, nvda.cash_and_equivalents, nvda.shares_outstanding = (
        6e11, 1e11, 4e10, 2.4e10,
    )
    session.commit()

    metrics = SqlStockSearchRepository(session).anchor_metrics_for_ticker("NVDA")
    assert metrics == AnchorMetrics(
        fcf_per_share=9.99,
        revenue_growth_yoy=15.5,
        eps_growth_yoy=22.0,
        ebitda=6e11,
        total_debt=1e11,
        cash_and_equivalents=4e10,
        shares_outstanding=2.4e10,
        market_cap=3e12,
        name="Nvidia",
    )


def test_anchor_metrics_for_ticker_all_none_when_unknown_or_unsynced(session):
    # No row, and a row present but not yet given the figures (nor a market cap or name),
    # both read as an empty AnchorMetrics — the analysis's DB-only overlay then simply omits
    # those reads.
    _seed(
        session,
        "NEW",
        market_cap=None,
        fcf_per_share=None,
        revenue_growth_yoy=None,
        eps_growth_yoy=None,
    )
    r = SqlStockSearchRepository(session)
    assert r.anchor_metrics_for_ticker("ZZZZ") == AnchorMetrics()  # no such anchor row
    assert r.anchor_metrics_for_ticker("NEW") == AnchorMetrics()  # row exists, unsynced


def test_tier_for_ticker_buckets_the_stored_cap(session):
    _seed(session, "NVDA", market_cap=3e12)  # >= $200B -> mega
    _seed(session, "AMD", market_cap=50e9)  # $10-200B -> large
    _seed(session, "MU", market_cap=5e9)  # $2-10B -> mid
    r = SqlStockSearchRepository(session)
    assert r.tier_for_ticker("NVDA") is MarketCapTier.MEGA
    assert r.tier_for_ticker("AMD") is MarketCapTier.LARGE
    assert r.tier_for_ticker("MU") is MarketCapTier.MID


def test_tier_for_ticker_none_when_unknown_or_below_the_smallest_tier(session):
    _seed(session, "TINY", market_cap=100e6)  # below the SMALL floor ($250M)
    r = SqlStockSearchRepository(session)
    assert r.tier_for_ticker("ZZZZ") is None  # no anchor row
    assert r.tier_for_ticker("TINY") is None  # too small to tier


def test_industry_peers_tags_each_mid_cap_and_up_peer_with_its_tier(session):
    _seed(session, "NVDA", industry="semiconductors", market_cap=3e12, pe_ratio=46.5)  # mega
    _seed(session, "AMD", industry="semiconductors", market_cap=50e9, pe_ratio=30.0)  # large
    _seed(session, "MU", industry="semiconductors", market_cap=5e9, pe_ratio=12.0)  # mid
    _seed(session, "SMALL", industry="semiconductors", market_cap=1.5e9, pe_ratio=90.0)  # <$2B — dropped
    _seed(session, "LOSS", industry="semiconductors", market_cap=5e9, pe_ratio=-5.0)  # loss — dropped
    r = SqlStockSearchRepository(session)

    peers = r.industry_peers("semiconductors")

    assert sorted(peers) == [
        (12.0, MarketCapTier.MID),
        (30.0, MarketCapTier.LARGE),
        (46.5, MarketCapTier.MEGA),
    ]


def test_peers_for_industry_returns_every_screened_row_with_its_tier(session):
    # Unlike industry_peers (the benchmark sample), the comparison candidates have NO P/E floor
    # and NO $2B floor — a comparison table shows every screened peer, tier scoping does the rest.
    _seed(
        session,
        "NVDA",
        industry="semiconductors",
        market_cap=3e12,
        pe_ratio=46.5,
        ev_ebitda=40.0,
        fcf_yield=1.8,
        revenue_growth_yoy=60.0,
    )  # mega
    _seed(session, "MU", industry="semiconductors", market_cap=5e9, pe_ratio=12.0)  # mid
    _seed(session, "SMALL", industry="semiconductors", market_cap=1.5e9, pe_ratio=90.0)  # <$2B — KEPT
    _seed(session, "LOSS", industry="semiconductors", market_cap=5e9, pe_ratio=None)  # no P/E — KEPT
    _seed(session, "XOM", industry="oil_gas_integrated", market_cap=4e11)  # other industry — excluded
    _seed(session, "PRIVATE", industry="semiconductors", market_cap=None)  # unscreened — excluded
    _row(session, "NVDA").net_margin = 55.0  # net_margin isn't a _seed param; set it directly
    session.commit()
    r = SqlStockSearchRepository(session)

    peers = {p.ticker: p for p in r.peers_for_industry("semiconductors")}

    assert set(peers) == {"NVDA", "MU", "SMALL", "LOSS"}  # other industry + unscreened dropped
    nvda = peers["NVDA"]
    assert (nvda.market_cap, nvda.pe_ratio, nvda.ev_ebitda, nvda.fcf_yield) == (3e12, 46.5, 40.0, 1.8)
    assert (nvda.net_margin, nvda.revenue_growth_yoy) == (55.0, 60.0)
    assert nvda.tier == MarketCapTier.MEGA
    assert peers["MU"].tier == MarketCapTier.MID
    assert peers["SMALL"].tier == MarketCapTier.SMALL  # the $1–2B tail is kept here


def test_peers_for_industry_empty_for_an_unknown_industry(session):
    _seed(session, "NVDA", industry="semiconductors", market_cap=3e12)
    r = SqlStockSearchRepository(session)

    assert r.peers_for_industry("nonesuch") == ()
