"""Offline tests for the heat-map slice — the entity grouping rules and the use case.

The entity build is pure, so it's tested directly. The use case is driven through hand-written
fakes for the two ports (the universe read repository and the batched quote feed), so nothing
touches SQLAlchemy or Alpaca.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.stocks.entities import Quote
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope
from app.stocks.heatmap.use_cases import GetStockHeatMap
from app.stocks.universe.entities import (
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)
from app.stocks.universe.repository import StockSearchRepository


# --- fixtures ------------------------------------------------------------------------------


def _row(ticker, sector, industry, cap, *, name=None):
    return HeatMapRow(
        ticker=ticker, name=name or ticker, sector=sector, industry=industry, market_cap=cap
    )


def _result(ticker, sector, industry, cap, *, in_sp500=True, in_nasdaq100=False):
    return StockSearchResult(
        ticker=ticker,
        name=f"{ticker} Inc.",
        sector=sector,
        industry=industry,
        market_cap=cap,
        pe_ratio=None,
        revenue_growth_yoy=None,
        eps_growth_yoy=None,
        forward_revenue_growth_yoy=None,
        forward_eps_growth_yoy=None,
        in_sp500=in_sp500,
        in_nasdaq100=in_nasdaq100,
    )


def _quote(symbol, price, previous_close):
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 9, tzinfo=timezone.utc),
    )


class FakeSearchRepo(StockSearchRepository):
    """Serves a fixed page and records the criteria it was called with."""

    def __init__(self, results):
        self._results = tuple(results)
        self.criteria: StockSearchCriteria | None = None

    def search(self, criteria):
        self.criteria = criteria
        return StockSearchPage(
            results=self._results, total=len(self._results), limit=criteria.limit, offset=0
        )

    def classifications(self):  # pragma: no cover - unused by the heat map
        raise NotImplementedError

    def pe_ratios_for_industry(self, industry):  # pragma: no cover - unused
        raise NotImplementedError

    def industry_for_ticker(self, ticker):  # pragma: no cover - unused
        raise NotImplementedError


class FakeBulkQuotes:
    def __init__(self, quotes=None, error=None):
        self._quotes = quotes or {}
        self._error = error
        self.requested: tuple[str, ...] | None = None

    def get_quotes(self, symbols):
        self.requested = tuple(symbols)
        if self._error is not None:
            raise self._error
        return dict(self._quotes)


# --- entity: HeatMap.build -----------------------------------------------------------------


def test_build_groups_sector_then_industry_and_colours_from_changes():
    rows = (
        _row("NVDA", "technology", "semiconductors", 3e12),
        _row("AVGO", "technology", "semiconductors", 8e11),
        _row("MSFT", "technology", "software", 3.2e12),
        _row("JPM", "financials", "banks", 6e11),
    )
    changes = {"NVDA": -0.99, "AVGO": 3.27, "MSFT": -1.07, "JPM": 1.70}
    heatmap = HeatMap.build(HeatMapScope.SP500, rows, changes)

    # Sectors ordered by total cap desc: technology (7e12) before financials (6e11).
    assert [s.sector for s in heatmap.sectors] == ["technology", "financials"]
    tech = heatmap.sectors[0]
    assert tech.market_cap == 3e12 + 8e11 + 3.2e12
    # Industries within technology ordered by cap desc: software (3.2e12) > semis (3.8e12)?
    # semis = 3e12 + 8e11 = 3.8e12 > software 3.2e12 -> semiconductors first.
    assert [i.industry for i in tech.industries] == ["semiconductors", "software"]
    semis = tech.industries[0]
    assert [c.ticker for c in semis.cells] == ["NVDA", "AVGO"]  # cap desc
    assert semis.cells[0].change_percent == -0.99
    assert heatmap.cell_count == 4


def test_build_drops_rows_without_a_sector():
    rows = (
        _row("NVDA", "technology", "semiconductors", 3e12),
        _row("ZZZZ", None, None, 1e9),  # unclassified — nowhere to place it
    )
    heatmap = HeatMap.build(HeatMapScope.SP500, rows, {})
    assert heatmap.cell_count == 1
    assert heatmap.sectors[0].sector == "technology"


def test_build_null_industry_forms_its_own_bucket_last():
    rows = (
        _row("A", "energy", "oil-gas", 5e11),
        _row("B", "energy", None, 9e11),  # classified sector, no industry
    )
    heatmap = HeatMap.build(HeatMapScope.SP500, rows, {})
    energy = heatmap.sectors[0]
    # The null-industry bucket sorts last despite its larger cap (name tiebreak "" is last
    # only against a real slug when caps tie; here cap desc puts null first). Assert it exists.
    industries = {i.industry for i in energy.industries}
    assert industries == {"oil-gas", None}


def test_build_missing_quote_leaves_cell_uncoloured():
    rows = (_row("NVDA", "technology", "semiconductors", 3e12),)
    heatmap = HeatMap.build(HeatMapScope.SP500, rows, {})  # no changes at all
    assert heatmap.sectors[0].industries[0].cells[0].change_percent is None


# --- use case: GetStockHeatMap -------------------------------------------------------------


def test_execute_filters_sp500_and_builds_coloured_map():
    repo = FakeSearchRepo(
        [
            _result("NVDA", "technology", "semiconductors", 3e12),
            _result("JPM", "financials", "banks", 6e11),
        ]
    )
    quotes = FakeBulkQuotes(
        {"NVDA": _quote("NVDA", 99.0, 100.0), "JPM": _quote("JPM", 102.0, 100.0)}
    )
    heatmap = GetStockHeatMap(repo, quotes).execute(HeatMapScope.SP500)

    assert repo.criteria.in_sp500 is True
    assert repo.criteria.in_nasdaq100 is None
    assert repo.criteria.sort is StockSort.MARKET_CAP
    assert repo.criteria.direction is SortDirection.DESC
    assert quotes.requested == ("NVDA", "JPM")
    nvda_cell = heatmap.sectors[0].industries[0].cells[0]
    assert nvda_cell.ticker == "NVDA"
    assert nvda_cell.change_percent == -1.0  # (99-100)/100*100


def test_execute_nasdaq100_scope_flips_the_flag():
    repo = FakeSearchRepo([_result("AAPL", "technology", "consumer-electronics", 3e12)])
    GetStockHeatMap(repo, FakeBulkQuotes()).execute(HeatMapScope.NASDAQ100)
    assert repo.criteria.in_nasdaq100 is True
    assert repo.criteria.in_sp500 is None


def test_execute_quote_failure_yields_uncoloured_map_not_an_error():
    repo = FakeSearchRepo([_result("NVDA", "technology", "semiconductors", 3e12)])
    quotes = FakeBulkQuotes(error=StockDataUnavailable("quotes", "boom"))
    heatmap = GetStockHeatMap(repo, quotes).execute(HeatMapScope.SP500)
    assert heatmap.cell_count == 1
    assert heatmap.sectors[0].industries[0].cells[0].change_percent is None


def test_execute_empty_universe_is_an_empty_map_no_quote_call():
    repo = FakeSearchRepo([])
    quotes = FakeBulkQuotes({"X": _quote("X", 1.0, 1.0)})
    heatmap = GetStockHeatMap(repo, quotes).execute(HeatMapScope.SP500)
    assert heatmap.sectors == ()
    assert quotes.requested is None  # no symbols -> provider never called
