"""Tests for the universe use cases: SyncUniverse (write side) + SearchStocks /
ListClassifications (read side).

Offline: hand-written fakes for the screener, classifier, and repository ports, so this
exercises only the orchestration — the upsert-vs-skip decision and the enrichment pass for the
sync, and the edge normalization (trim/slug/clamp) and criteria pass-through for the search —
independent of Yahoo or the DB.
"""

import pytest

from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import (
    Classifications,
    CompanyClassification,
    MarketCapTier,
    ScreenedStock,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)
from app.stocks.universe.ports import CompanyClassificationProvider, StockScreener
from app.stocks.universe.repository import (
    StockSearchRepository,
    UniverseRepository,
    UniverseSyncCounts,
)
from app.stocks.universe.use_cases import (
    GetIndustryValuation,
    ListClassifications,
    SearchStocks,
    SyncUniverse,
    UniverseSyncReport,
)


def _stock(ticker, *, market_cap=1e10, name=None, exchange=None, sector=None, price=None):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
        price=price,
    )


def _a_screen(n: int) -> tuple[ScreenedStock, ...]:
    """A plausible screen of ``n`` distinct names, each above the floor (no price, so the
    valuation pass skips them — the priced names a valuation test cares about are added on
    top)."""
    return tuple(_stock(f"T{i:04d}", market_cap=5e9 + i) for i in range(n))


def _four_quarter_timeline(symbol: str, ttm_eps: float) -> QuarterlyEarningsTimeline:
    """A stored timeline whose ``ttm_eps`` is exactly ``ttm_eps`` — four reported quarters
    each carrying a quarter of it (the shape the valuation pass reads through the port)."""
    per_q = ttm_eps / 4
    quarters = tuple(
        QuarterlyEarnings(
            fiscal_year=2025,
            fiscal_quarter=q,
            period_end=None,
            report_date=None,
            eps_actual=per_q,
            eps_estimate=None,
            eps_surprise=None,
            eps_surprise_percent=None,
            revenue_estimate=None,
            revenue_actual=None,
        )
        for q in range(1, 5)
    )
    return QuarterlyEarningsTimeline(symbol=symbol, quarters=quarters)


class _FakeQuarterlyRepo(QuarterlyEarningsRepository):
    """Serves a canned TTM EPS per ticker for the valuation pass; a ticker absent from the map
    reads as un-cached (``get`` returns ``None``)."""

    def __init__(self, ttm_by_ticker=None) -> None:
        self._ttm = dict(ttm_by_ticker or {})
        self.gets: list[str] = []

    def get(self, symbol):
        self.gets.append(symbol)
        if symbol not in self._ttm:
            return None
        return _four_quarter_timeline(symbol, self._ttm[symbol])

    def upsert(self, symbol, name, timeline):  # unused by the valuation pass
        raise NotImplementedError

    def refresh_targets(self, limit):  # unused by the valuation pass
        raise NotImplementedError


class _FakeScreener(StockScreener):
    """Returns a canned screen, or raises the given error."""

    def __init__(self, stocks=(), *, error=None) -> None:
        self._stocks = tuple(stocks)
        self._error = error
        self.calls: list[float] = []

    def screen(self, *, min_market_cap):
        self.calls.append(min_market_cap)
        if self._error is not None:
            raise self._error
        return self._stocks


class _FakeClassifier(CompanyClassificationProvider):
    """Maps ticker -> classification; raises StockDataUnavailable for tickers in ``errors``."""

    def __init__(self, mapping=None, *, errors=()) -> None:
        self._mapping = dict(mapping or {})
        self._errors = set(errors)
        self.calls: list[str] = []

    def get_classification(self, symbol):
        self.calls.append(symbol)
        if symbol in self._errors:
            raise StockDataUnavailable(symbol, "yahoo blocked")
        return self._mapping.get(symbol, CompanyClassification())


class _FakeRepo(UniverseRepository):
    """Records the upsert input and the classifications written; serves a canned work-list."""

    def __init__(self, *, counts=UniverseSyncCounts(0, 0), missing=()) -> None:
        self._counts = counts
        self._missing = tuple(missing)
        self.upserted: tuple[ScreenedStock, ...] | None = None
        self.classified: list[tuple[str, CompanyClassification]] = []
        self.missing_limit: int | None = None
        # The {ticker: pe} map handed to set_pe_ratios — None until the valuation pass runs.
        self.pe_written: dict[str, float | None] | None = None

    def upsert_screen(self, stocks):
        self.upserted = tuple(stocks)
        return self._counts

    def tickers_missing_classification(self, limit):
        self.missing_limit = limit
        return self._missing

    def set_classification(self, ticker, classification):
        self.classified.append((ticker, classification))

    def set_pe_ratios(self, pe_by_ticker):
        self.pe_written = dict(pe_by_ticker)
        return sum(1 for pe in self.pe_written.values() if pe is not None)


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=UniverseSyncCounts(added=3, updated=7))

    report = SyncUniverse(screener, repo, _FakeClassifier()).execute()

    assert isinstance(report, UniverseSyncReport)
    assert screener.calls == [SyncUniverse.MIN_MARKET_CAP]  # the floor is passed through
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 3, 7)
    assert report.skipped is False
    assert (report.enriched, report.enrich_failed) == (0, 0)  # nothing missing to classify


def test_sync_skips_an_empty_screen_without_touching_the_store():
    screener = _FakeScreener(())
    repo = _FakeRepo()
    classifier = _FakeClassifier()
    quarterly = _FakeQuarterlyRepo()

    report = SyncUniverse(screener, repo, classifier, quarterly).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed, report.valued) == (0, 0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment AND valuation passes are skipped too — a blocked bulk screen means
    # blocked .info calls, and there's nothing fresh to value.
    assert repo.missing_limit is None
    assert classifier.calls == []
    assert repo.pe_written is None  # valuation pass never ran
    assert quarterly.gets == []


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    screener = _FakeScreener(_a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN - 1))
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo, _FakeClassifier(), _FakeQuarterlyRepo()).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.missing_limit is None  # enrichment not reached
    assert repo.pe_written is None  # valuation not reached either
    assert report.valued == 0


def test_sync_propagates_a_hard_screen_failure():
    screener = _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked"))
    repo = _FakeRepo()

    with pytest.raises(StockDataUnavailable):
        SyncUniverse(screener, repo, _FakeClassifier()).execute()
    assert repo.upserted is None  # nothing written on a hard failure
    assert repo.missing_limit is None


def test_sync_enriches_stocks_missing_an_industry():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("AAPL", "MSFT"))
    classifier = _FakeClassifier(
        {
            "AAPL": CompanyClassification("technology", "consumer_electronics"),
            "MSFT": CompanyClassification("technology", "software_infrastructure"),
        }
    )

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    assert classifier.calls == ["AAPL", "MSFT"]
    assert repo.classified == [
        ("AAPL", CompanyClassification("technology", "consumer_electronics")),
        ("MSFT", CompanyClassification("technology", "software_infrastructure")),
    ]
    assert (report.enriched, report.enrich_failed) == (2, 0)


def test_enrichment_counts_a_source_failure_and_keeps_going():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("AAPL", "BADX", "MSFT"))
    classifier = _FakeClassifier(
        {
            "AAPL": CompanyClassification(industry="consumer_electronics"),
            "MSFT": CompanyClassification(industry="software_infrastructure"),
        },
        errors=("BADX",),
    )

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    # BADX raised, so it isn't written — but the sweep continued to MSFT.
    assert [ticker for ticker, _ in repo.classified] == ["AAPL", "MSFT"]
    assert (report.enriched, report.enrich_failed) == (2, 1)


def test_enrichment_leaves_an_unclassifiable_symbol_for_later():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("ETF",))
    # The source reached the symbol but has no sector/industry for it (both None).
    classifier = _FakeClassifier({"ETF": CompanyClassification()})

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    assert repo.classified == []  # nothing written
    # Neither enriched nor failed — nothing went wrong, it's just left for a later run.
    assert (report.enriched, report.enrich_failed) == (0, 0)


def test_enrichment_limit_defaults_then_overrides():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)

    repo = _FakeRepo()
    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()
    assert repo.missing_limit == SyncUniverse.DEFAULT_LIMIT

    repo = _FakeRepo()
    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute(limit=25)
    assert repo.missing_limit == 25


def test_sync_values_screened_stocks_with_the_card_pe():
    # Two priced names on top of a plausible screen: one with four cached quarters (a TTM),
    # one un-cached. The baseline names carry no price, so the valuation pass skips them.
    priced = (
        _stock("AAPL", market_cap=3e12, price=100.0),
        _stock("MSFT", market_cap=2e12, price=50.0),
    )
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + priced
    repo = _FakeRepo()
    quarterly = _FakeQuarterlyRepo({"AAPL": 5.0})  # AAPL TTM 5 -> 100/5 = 20; MSFT un-cached

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    # Only the priced names are written; the price-less baseline is skipped, not nulled.
    assert set(repo.pe_written) == {"AAPL", "MSFT"}
    assert repo.pe_written["AAPL"] == 20.0  # price / TTM EPS — the card's exact rule
    assert repo.pe_written["MSFT"] is None  # priced but no cached quarters -> no P/E
    assert report.valued == 1  # only AAPL got a non-null figure


def test_sync_nulls_the_pe_for_a_trailing_loss():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("LOSS", market_cap=1e10, price=30.0),
    )
    repo = _FakeRepo()
    quarterly = _FakeQuarterlyRepo({"LOSS": -2.0})  # negative TTM -> a P/E is meaningless

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    assert repo.pe_written["LOSS"] is None
    assert report.valued == 0


def test_sync_writes_no_pe_when_no_quarterly_cache_is_wired():
    # P/E is best-effort enrichment: with no quarterly repo the sync still screens/classifies
    # but the valuation pass is a no-op — set_pe_ratios is never called.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("AAPL", market_cap=3e12, price=100.0),
    )
    repo = _FakeRepo()

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.pe_written is None  # set_pe_ratios never called
    assert report.valued == 0
    assert report.skipped is False  # the screen itself still succeeded


# --- SearchStocks / ListClassifications (the read side) ------------------------------------

_RESULT = StockSearchResult(
    ticker="NVDA",
    name="Nvidia",
    sector="technology",
    industry="semiconductors",
    market_cap=3e12,
    pe_ratio=48.2,
    revenue_growth_yoy=61.6,
    eps_growth_yoy=587.4,
    forward_revenue_growth_yoy=52.1,
    forward_eps_growth_yoy=48.3,
    in_sp500=True,
    in_nasdaq100=True,
)


class _FakeSearchRepo(StockSearchRepository):
    """Records the criteria it was handed and returns a canned page / classifications /
    per-industry P/E list."""

    def __init__(
        self, *, page=None, classifications=None, pe_ratios=(), industry=None
    ) -> None:
        self._page = page or StockSearchPage(results=(), total=0, limit=0, offset=0)
        self._classifications = classifications or Classifications((), ())
        self._pe_ratios = pe_ratios
        self._industry = industry
        self.criteria: StockSearchCriteria | None = None
        self.classifications_calls = 0
        self.industry_asked: str | None = None
        self.ticker_asked: str | None = None

    def search(self, criteria):
        self.criteria = criteria
        return self._page

    def classifications(self):
        self.classifications_calls += 1
        return self._classifications

    def pe_ratios_for_industry(self, industry):
        self.industry_asked = industry
        return tuple(self._pe_ratios)

    def industry_for_ticker(self, ticker):
        self.ticker_asked = ticker
        return self._industry


def test_search_normalizes_inputs_and_passes_clean_criteria():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(
        query="  NvDa ",
        sectors=["Consumer Electronics"],
        industries=["  Semiconductors  "],
        in_sp500=True,
        in_nasdaq100=False,
        market_cap_tiers=[MarketCapTier.LARGE],
        sort=StockSort.REVENUE_GROWTH,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "NvDa"
    assert c.sectors == ("consumer_electronics",)  # slugged to the stored convention
    assert c.industries == ("semiconductors",)  # slugged + trimmed
    assert (c.in_sp500, c.in_nasdaq100) == (True, False)
    assert c.market_cap_tiers == (MarketCapTier.LARGE,)  # enum passes straight through
    assert (c.sort, c.direction) == (StockSort.REVENUE_GROWTH, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_multi_select_slugs_dedupes_and_drops_blanks():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(
        sectors=["Technology", "  technology  ", "", "Energy"],
        industries=["Semiconductors", "semiconductors"],
        market_cap_tiers=[MarketCapTier.LARGE, MarketCapTier.MID, MarketCapTier.LARGE],
    )
    c = repo.criteria
    # Each label slugged; blanks dropped; duplicates collapsed with first-seen order kept.
    assert c.sectors == ("technology", "energy")
    assert c.industries == ("semiconductors",)
    assert c.market_cap_tiers == (MarketCapTier.LARGE, MarketCapTier.MID)


def test_search_blank_text_and_filters_become_empty():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(query="   ", sectors=["", "   "], industries=None)
    c = repo.criteria
    assert c.query is None
    # Multi-select filters normalize to an empty tuple ("don't filter on this axis").
    assert (c.sectors, c.industries, c.market_cap_tiers) == ((), (), ())
    # Index flags default to a tri-state "don't filter".
    assert (c.in_sp500, c.in_nasdaq100) == (None, None)


def test_search_defaults_to_no_sort_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute()
    c = repo.criteria
    # No sort by default — the repository orders an unsorted browse by ticker (A→Z). The
    # direction default (descending) rides along unused until a sort field is chosen.
    assert c.sort is None
    assert c.direction is SortDirection.DESC
    assert (c.limit, c.offset) == (SearchStocks.DEFAULT_LIMIT, 0)
    assert c.query is None
    assert c.market_cap_tiers == ()  # no tier filter unless asked


@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 1),
        (-5, 1),
        (1, 1),
        (50, 50),
        (SearchStocks.MAX_LIMIT, SearchStocks.MAX_LIMIT),
        (SearchStocks.MAX_LIMIT + 1, SearchStocks.MAX_LIMIT),
        (10_000, SearchStocks.MAX_LIMIT),
    ],
)
def test_search_clamps_limit_into_range(given, expected):
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(limit=given)
    assert repo.criteria.limit == expected


def test_search_floors_a_negative_offset():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(offset=-3)
    assert repo.criteria.offset == 0


def test_search_returns_the_repository_page_unchanged():
    page = StockSearchPage(results=(_RESULT,), total=1, limit=25, offset=0)
    repo = _FakeSearchRepo(page=page)
    assert SearchStocks(repo).execute(query="nv") is page


def test_list_classifications_passes_through():
    classifications = Classifications(
        ("energy", "technology"), ("oil_gas", "semiconductors")
    )
    repo = _FakeSearchRepo(classifications=classifications)

    result = ListClassifications(repo).execute()

    assert result is classifications
    assert repo.classifications_calls == 1


# --- GetIndustryValuation (the per-industry P/E benchmark) ---------------------------------


def test_industry_valuation_slugs_the_industry_and_summarizes_peers():
    repo = _FakeSearchRepo(pe_ratios=(10.0, 20.0, 30.0, 40.0, 50.0))
    result = GetIndustryValuation(repo).execute("  Semiconductors ")
    # The raw label is slugged + trimmed before the read, so a slug or a label both work.
    assert repo.industry_asked == "semiconductors"
    assert result.industry == "semiconductors"
    assert result.count == 5
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (30.0, 20.0, 40.0)


def test_industry_valuation_interpolates_quartiles_on_an_even_sample():
    # Four peers: the median sits between the two middles, the quartiles interpolate.
    result = GetIndustryValuation(
        _FakeSearchRepo(pe_ratios=(10.0, 20.0, 30.0, 40.0))
    ).execute("x")
    assert result.median_pe == 25.0  # (20 + 30) / 2
    assert result.p25_pe == 17.5
    assert result.p75_pe == 32.5


def test_industry_valuation_single_peer_is_its_own_median():
    result = GetIndustryValuation(_FakeSearchRepo(pe_ratios=(18.0,))).execute("x")
    assert result.count == 1
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (18.0, 18.0, 18.0)


def test_industry_valuation_empty_when_no_peers():
    # An unknown but well-formed industry has no peers — a valid benchmark, not an error.
    result = GetIndustryValuation(_FakeSearchRepo(pe_ratios=())).execute("nonesuch")
    assert result.count == 0
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (None, None, None)


def test_industry_valuation_rejects_a_blank_industry():
    with pytest.raises(ValueError):
        GetIndustryValuation(_FakeSearchRepo()).execute("   ")
