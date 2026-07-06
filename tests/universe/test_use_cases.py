"""Tests for the universe use cases: SyncUniverse (write side) + SearchStocks /
ListClassifications (read side).

Offline: hand-written fakes for the screener, classifier, and repository ports, so this
exercises only the orchestration — the upsert-vs-skip decision and the enrichment pass for the
sync, and the edge normalization (trim/slug/clamp) and criteria pass-through for the search —
independent of Yahoo or the DB.
"""

import pytest

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
    ListClassifications,
    SearchStocks,
    SyncUniverse,
    UniverseSyncReport,
)


def _stock(ticker, *, market_cap=1e10, name=None, exchange=None, sector=None):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
    )


def _a_screen(n: int) -> tuple[ScreenedStock, ...]:
    """A plausible screen of ``n`` distinct names, each above the floor."""
    return tuple(_stock(f"T{i:04d}", market_cap=5e9 + i) for i in range(n))


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

    def upsert_screen(self, stocks):
        self.upserted = tuple(stocks)
        return self._counts

    def tickers_missing_classification(self, limit):
        self.missing_limit = limit
        return self._missing

    def set_classification(self, ticker, classification):
        self.classified.append((ticker, classification))


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

    report = SyncUniverse(screener, repo, classifier).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed) == (0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment pass is skipped too — a blocked bulk screen means blocked .info calls.
    assert repo.missing_limit is None
    assert classifier.calls == []


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    screener = _FakeScreener(_a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN - 1))
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo, _FakeClassifier()).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.missing_limit is None  # enrichment not reached


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


# --- SearchStocks / ListClassifications (the read side) ------------------------------------

_RESULT = StockSearchResult(
    ticker="NVDA",
    name="Nvidia",
    sector="technology",
    industry="semiconductors",
    market_cap=3e12,
    revenue_growth_yoy=61.6,
    eps_growth_yoy=587.4,
    forward_revenue_growth_yoy=52.1,
    forward_eps_growth_yoy=48.3,
    in_sp500=True,
    in_nasdaq100=True,
)


class _FakeSearchRepo(StockSearchRepository):
    """Records the criteria it was handed and returns a canned page / classifications."""

    def __init__(self, *, page=None, classifications=None) -> None:
        self._page = page or StockSearchPage(results=(), total=0, limit=0, offset=0)
        self._classifications = classifications or Classifications((), ())
        self.criteria: StockSearchCriteria | None = None
        self.classifications_calls = 0

    def search(self, criteria):
        self.criteria = criteria
        return self._page

    def classifications(self):
        self.classifications_calls += 1
        return self._classifications


def test_search_normalizes_inputs_and_passes_clean_criteria():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(
        query="  NvDa ",
        sector="Consumer Electronics",
        industry="  Semiconductors  ",
        in_sp500=True,
        in_nasdaq100=False,
        market_cap_tier=MarketCapTier.LARGE,
        sort=StockSort.REVENUE_GROWTH,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "NvDa"
    assert c.sector == "consumer_electronics"  # slugged to the stored convention
    assert c.industry == "semiconductors"  # slugged + trimmed
    assert (c.in_sp500, c.in_nasdaq100) == (True, False)
    assert c.market_cap_tier is MarketCapTier.LARGE  # enum passes straight through
    assert (c.sort, c.direction) == (StockSort.REVENUE_GROWTH, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_blank_text_and_filters_become_none():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(query="   ", sector="", industry=None)
    c = repo.criteria
    assert (c.query, c.sector, c.industry) == (None, None, None)
    # Index flags default to a tri-state "don't filter".
    assert (c.in_sp500, c.in_nasdaq100) == (None, None)


def test_search_defaults_to_market_cap_desc_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute()
    c = repo.criteria
    assert (c.sort, c.direction) == (StockSort.MARKET_CAP, SortDirection.DESC)
    assert (c.limit, c.offset) == (SearchStocks.DEFAULT_LIMIT, 0)
    assert c.query is None
    assert c.market_cap_tier is None  # no tier filter unless asked


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
