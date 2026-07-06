"""Tests for the ETF use cases: SyncEtfs (write side) + SearchEtfs / ListEtfCategories (read).

Offline: hand-written fakes for the screener, classifier, and repository ports, so this exercises
only the orchestration — the upsert-vs-skip decision and the category enrichment pass for the
sync, and the edge normalization (trim/slug/clamp) and criteria pass-through for the search —
independent of Yahoo or the DB.
"""

import pytest

from app.stocks.etfs.entities import (
    EtfCategories,
    EtfClassification,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.etfs.ports import EtfCategoryProvider, EtfScreener
from app.stocks.etfs.repository import (
    EtfRepository,
    EtfSearchRepository,
    EtfSyncCounts,
)
from app.stocks.etfs.use_cases import (
    EtfSyncReport,
    ListEtfCategories,
    SearchEtfs,
    SyncEtfs,
)
from app.stocks.exceptions import StockDataUnavailable


def _etf(ticker, *, net_assets=1e10):
    return ScreenedEtf(ticker=ticker, net_assets=net_assets)


def _a_screen(n: int) -> tuple[ScreenedEtf, ...]:
    """A plausible screen of ``n`` distinct funds."""
    return tuple(_etf(f"E{i:04d}", net_assets=1e9 + i) for i in range(n))


class _FakeScreener(EtfScreener):
    """Returns a canned screen, or raises the given error."""

    def __init__(self, etfs=(), *, error=None) -> None:
        self._etfs = tuple(etfs)
        self._error = error
        self.calls = 0

    def screen(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._etfs


class _FakeClassifier(EtfCategoryProvider):
    """Maps ticker -> classification; raises StockDataUnavailable for tickers in ``errors``."""

    def __init__(self, mapping=None, *, errors=()) -> None:
        self._mapping = dict(mapping or {})
        self._errors = set(errors)
        self.calls: list[str] = []

    def get_category(self, symbol):
        self.calls.append(symbol)
        if symbol in self._errors:
            raise StockDataUnavailable(symbol, "yahoo blocked")
        return self._mapping.get(symbol, EtfClassification())


class _FakeRepo(EtfRepository):
    """Records the upsert input and the categories written; serves a canned work-list."""

    def __init__(self, *, counts=EtfSyncCounts(0, 0), missing=()) -> None:
        self._counts = counts
        self._missing = tuple(missing)
        self.upserted: tuple[ScreenedEtf, ...] | None = None
        self.categorised: list[tuple[str, EtfClassification]] = []
        self.missing_limit: int | None = None

    def upsert_screen(self, etfs):
        self.upserted = tuple(etfs)
        return self._counts

    def tickers_missing_category(self, limit):
        self.missing_limit = limit
        return self._missing

    def set_category(self, ticker, classification):
        self.categorised.append((ticker, classification))


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=EtfSyncCounts(added=5, updated=45))

    report = SyncEtfs(screener, repo, _FakeClassifier()).execute()

    assert isinstance(report, EtfSyncReport)
    assert screener.calls == 1
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 5, 45)
    assert report.skipped is False
    assert (report.enriched, report.enrich_failed) == (0, 0)  # nothing missing to categorise


def test_sync_skips_an_empty_screen_without_touching_the_store():
    repo = _FakeRepo()
    classifier = _FakeClassifier()

    report = SyncEtfs(_FakeScreener(()), repo, classifier).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed) == (0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment pass is skipped too — a blocked bulk screen means blocked .info calls.
    assert repo.missing_limit is None
    assert classifier.calls == []


def test_sync_skips_an_implausibly_small_screen():
    repo = _FakeRepo()
    report = SyncEtfs(
        _FakeScreener(_a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN - 1)), repo, _FakeClassifier()
    ).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.missing_limit is None  # enrichment not reached


def test_sync_propagates_a_hard_screen_failure():
    repo = _FakeRepo()
    with pytest.raises(StockDataUnavailable):
        SyncEtfs(
            _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked")),
            repo,
            _FakeClassifier(),
        ).execute()
    assert repo.upserted is None  # nothing written on a hard failure
    assert repo.missing_limit is None


def test_sync_enriches_funds_missing_a_category():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("SPY", "QQQ"))
    classifier = _FakeClassifier(
        {
            "SPY": EtfClassification("large_blend"),
            "QQQ": EtfClassification("large_growth"),
        }
    )

    report = SyncEtfs(_FakeScreener(screen), repo, classifier).execute()

    assert classifier.calls == ["SPY", "QQQ"]
    assert repo.categorised == [
        ("SPY", EtfClassification("large_blend")),
        ("QQQ", EtfClassification("large_growth")),
    ]
    assert (report.enriched, report.enrich_failed) == (2, 0)


def test_enrichment_counts_a_source_failure_and_keeps_going():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("SPY", "BADX", "QQQ"))
    classifier = _FakeClassifier(
        {"SPY": EtfClassification("large_blend"), "QQQ": EtfClassification("large_growth")},
        errors=("BADX",),
    )

    report = SyncEtfs(_FakeScreener(screen), repo, classifier).execute()

    # BADX raised, so it isn't written — but the sweep continued to QQQ.
    assert [ticker for ticker, _ in repo.categorised] == ["SPY", "QQQ"]
    assert (report.enriched, report.enrich_failed) == (2, 1)


def test_enrichment_leaves_an_uncategorisable_fund_for_later():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("WEIRD",))
    # The source reached the fund but has no category for it.
    classifier = _FakeClassifier({"WEIRD": EtfClassification()})

    report = SyncEtfs(_FakeScreener(screen), repo, classifier).execute()

    assert repo.categorised == []  # nothing written
    # Neither enriched nor failed — nothing went wrong, it's just left for a later run.
    assert (report.enriched, report.enrich_failed) == (0, 0)


def test_enrichment_limit_defaults_then_overrides():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)

    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeClassifier()).execute()
    assert repo.missing_limit == SyncEtfs.DEFAULT_LIMIT

    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeClassifier()).execute(limit=25)
    assert repo.missing_limit == 25


# --- SearchEtfs / ListEtfCategories (the read side) ----------------------------------------

_RESULT = EtfSearchResult(
    ticker="SPY",
    name="SPDR S&P 500 ETF Trust",
    exchange="NYSEARCA",
    net_assets=5e11,
    expense_ratio=0.09,
    category="large_blend",
)


class _FakeSearchRepo(EtfSearchRepository):
    """Records the criteria it was handed and returns a canned page / categories."""

    def __init__(self, *, page=None, categories=None) -> None:
        self._page = page or EtfSearchPage(results=(), total=0, limit=0, offset=0)
        self._categories = categories or EtfCategories(())
        self.criteria = None
        self.categories_calls = 0

    def search(self, criteria):
        self.criteria = criteria
        return self._page

    def categories(self):
        self.categories_calls += 1
        return self._categories


def test_search_normalizes_inputs_and_passes_clean_criteria():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(
        query="  Gold ",
        category="Large Growth",
        sort=EtfSort.EXPENSE_RATIO,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "Gold"
    assert c.category == "large_growth"  # slugged to the stored convention
    assert (c.sort, c.direction) == (EtfSort.EXPENSE_RATIO, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_blank_text_and_category_become_none():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(query="   ", category="")
    c = repo.criteria
    assert (c.query, c.category) == (None, None)


def test_search_defaults_to_net_assets_desc_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute()
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)
    assert (c.limit, c.offset) == (SearchEtfs.DEFAULT_LIMIT, 0)
    assert (c.query, c.category) == (None, None)


@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 1),
        (-5, 1),
        (1, 1),
        (50, 50),
        (SearchEtfs.MAX_LIMIT, SearchEtfs.MAX_LIMIT),
        (SearchEtfs.MAX_LIMIT + 1, SearchEtfs.MAX_LIMIT),
        (10_000, SearchEtfs.MAX_LIMIT),
    ],
)
def test_search_clamps_limit_into_range(given, expected):
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(limit=given)
    assert repo.criteria.limit == expected


def test_search_floors_a_negative_offset():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(offset=-3)
    assert repo.criteria.offset == 0


def test_search_returns_the_repository_page_unchanged():
    page = EtfSearchPage(results=(_RESULT,), total=1, limit=25, offset=0)
    repo = _FakeSearchRepo(page=page)
    assert SearchEtfs(repo).execute(query="spy") is page


def test_list_categories_passes_through():
    categories = EtfCategories(("commodities_focused", "large_blend", "large_growth"))
    repo = _FakeSearchRepo(categories=categories)

    result = ListEtfCategories(repo).execute()

    assert result is categories
    assert repo.categories_calls == 1
