"""Tests for the ETF use cases: SyncEtfs (write side) + SearchEtfs / ListEtfCategories /
GetEtfDetail (read).

Offline: hand-written fakes for the screener, classifier, quote, profile, and repository ports, so
this exercises only the orchestration — the upsert-vs-skip decision and the category enrichment
pass for the sync, the edge normalization (trim/slug/clamp) and criteria pass-through for the
search, and for the detail: the membership gate (404 before any upstream call), the quote-primary
propagation, and the best-effort profile degradation — independent of Yahoo, Alpaca, or the DB.
"""

from datetime import datetime, timezone

import pytest

from app.stocks.entities import Quote
from app.stocks.etfs.entities import (
    EtfCategories,
    EtfClassification,
    EtfHolding,
    EtfProfile,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    EtfSectorWeight,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.etfs.ports import EtfCategoryProvider, EtfProfileProvider, EtfScreener
from app.stocks.etfs.repository import (
    EtfLookupRepository,
    EtfRepository,
    EtfSearchRepository,
    EtfSyncCounts,
)
from app.stocks.etfs.use_cases import (
    EtfSyncReport,
    GetEtfDetail,
    ListEtfCategories,
    SearchEtfs,
    SyncEtfs,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockQuoteProvider


def _etf(ticker, *, net_assets=1e10):
    return ScreenedEtf(ticker=ticker, net_assets=net_assets)


def _a_screen(n: int) -> tuple[ScreenedEtf, ...]:
    """A plausible screen of ``n`` distinct funds."""
    return tuple(_etf(f"E{i:04d}", net_assets=1e9 + i) for i in range(n))


class _FakeScreener(EtfScreener):
    """Returns a canned screen, or raises the given error; records the AUM floor it was asked for."""

    def __init__(self, etfs=(), *, error=None) -> None:
        self._etfs = tuple(etfs)
        self._error = error
        self.calls = 0
        self.min_net_assets: float | None = None

    def screen(self, *, min_net_assets):
        self.calls += 1
        self.min_net_assets = min_net_assets
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
    assert screener.min_net_assets == SyncEtfs.MIN_NET_ASSETS  # the AUM floor is threaded through
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


def test_enrichment_defaults_to_no_limit_then_overrides():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)

    # Default: uncapped — the enrichment pass asks the repo for every uncategorised fund.
    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeClassifier()).execute()
    assert repo.missing_limit is None

    # An explicit limit still caps a run — the throttle escape hatch.
    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeClassifier()).execute(limit=25)
    assert repo.missing_limit == 25


# --- SearchEtfs / ListEtfCategories (the read side) ----------------------------------------

_RESULT = EtfSearchResult(
    ticker="SPY",
    name="SPDR S&P 500 ETF Trust",
    exchange="NYSE",
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


# --- GetEtfDetail -------------------------------------------------------------------------


def _facts(ticker="VOO", **overrides) -> EtfSearchResult:
    base = dict(
        name="Vanguard S&P 500 ETF",
        exchange="NYSE",
        net_assets=1.7e12,
        expense_ratio=0.03,
        category="large_blend",
    )
    base.update(overrides)
    return EtfSearchResult(ticker=ticker, **base)


def _quote(symbol="VOO", price=685.28, previous_close=682.07) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
    )


def _a_profile() -> EtfProfile:
    return EtfProfile(
        fund_family="Vanguard",
        net_assets=1.8e12,  # a wrong-answer sentinel: the table's net_assets must win
        expense_ratio=0.05,  # sentinel: the table's expense_ratio must win
        nav=685.28,
        dividend_yield=1.03,
        ytd_return=11.25,
        three_year_return=20.41,
        five_year_return=13.01,
        description="An S&P 500 index fund.",
        top_holdings=(EtfHolding(ticker="NVDA", name="NVIDIA Corp", weight=7.89),),
        sector_weightings=(EtfSectorWeight(sector="technology", weight=39.13),),
    )


class _FakeLookup(EtfLookupRepository):
    """In-memory single-fund lookup; records the get/is_etf calls."""

    def __init__(self, facts: EtfSearchResult | None) -> None:
        self._facts = facts
        self.get_calls: list[str] = []

    def is_etf(self, ticker: str) -> bool:
        return self._facts is not None

    def get(self, ticker: str) -> EtfSearchResult | None:
        self.get_calls.append(ticker)
        return self._facts


class _FakeQuotes(StockQuoteProvider):
    def __init__(self, quote: Quote | None = None, error: Exception | None = None) -> None:
        self._quote = quote
        self._error = error
        self.calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._quote or _quote(symbol)


class _FakeProfileProvider(EtfProfileProvider):
    def __init__(self, profile: EtfProfile | None = None, error: Exception | None = None) -> None:
        self._profile = profile if profile is not None else EtfProfile.empty()
        self._error = error
        self.calls: list[str] = []

    def get_profile(self, symbol: str) -> EtfProfile:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._profile


_UNSET = object()  # sentinel so an explicit facts=None (non-ETF) differs from "not passed"


def _detail_use_case(
    *, facts=_UNSET, quote=None, quote_error=None, profile=None, profile_error=None
):
    lookup = _FakeLookup(_facts() if facts is _UNSET else facts)
    quotes = _FakeQuotes(quote, quote_error)
    prof = _FakeProfileProvider(profile, profile_error)
    return GetEtfDetail(lookup, quotes, prof), lookup, quotes, prof


def test_detail_assembles_quote_stored_facts_and_profile():
    use_case, _, quotes, prof = _detail_use_case(
        quote=_quote(), profile=_a_profile()
    )

    detail = use_case.execute("voo")  # lower-case in -> normalized

    assert detail.ticker == "VOO"
    # Quote-derived fields (primary source), the same change rules as every price view.
    assert detail.quote.price == 685.28
    assert detail.quote.change == pytest.approx(3.21)
    assert detail.quote.change_percent == pytest.approx(0.47, abs=0.01)
    # Stored etfs-table facts.
    assert detail.name == "Vanguard S&P 500 ETF"
    assert detail.exchange == "NYSE"
    assert detail.category == "large_blend"
    # The table's net_assets / expense_ratio win over the profile's (the detail page must agree
    # with the screener list).
    assert detail.net_assets == 1.7e12
    assert detail.expense_ratio == 0.03
    # Best-effort profile enrichment rides along.
    assert detail.profile.fund_family == "Vanguard"
    assert detail.profile.top_holdings[0].ticker == "NVDA"
    assert quotes.calls == ["VOO"]
    assert prof.calls == ["VOO"]


def test_detail_falls_back_to_profile_figures_when_the_table_lacks_them():
    # A fund the table has no net_assets/expense_ratio for yet: the profile fills the gap.
    use_case, *_ = _detail_use_case(
        facts=_facts(net_assets=None, expense_ratio=None),
        profile=_a_profile(),  # carries net_assets=1.8e12, expense_ratio=0.05
    )

    detail = use_case.execute("VOO")

    assert detail.net_assets == 1.8e12
    assert detail.expense_ratio == 0.05


def test_detail_404s_before_any_upstream_call_for_a_non_etf():
    use_case, lookup, quotes, prof = _detail_use_case(facts=None)  # not in the universe

    with pytest.raises(StockNotFound):
        use_case.execute("AAPL")

    # The membership gate short-circuits: neither the quote nor the profile was fetched.
    assert lookup.get_calls == ["AAPL"]
    assert quotes.calls == []
    assert prof.calls == []


def test_detail_propagates_a_quote_failure():
    # The quote is primary — its failure propagates (mapped to 502 at the edge), not degraded.
    use_case, _, _, prof = _detail_use_case(
        quote_error=StockDataUnavailable("VOO", "alpaca down")
    )

    with pytest.raises(StockDataUnavailable):
        use_case.execute("VOO")

    # The profile is never reached once the primary source has failed.
    assert prof.calls == []


def test_detail_degrades_to_an_empty_profile_when_yahoo_is_unavailable():
    # Best-effort enrichment: even a (contract-breaking) raising profile provider never sinks the
    # card — the quote + stored facts still serve on a 200-worthy result with an empty profile.
    use_case, *_ = _detail_use_case(
        quote=_quote(),
        profile_error=StockDataUnavailable("VOO", "yahoo blocked"),
    )

    detail = use_case.execute("VOO")

    assert detail.profile == EtfProfile.empty()
    assert detail.name == "Vanguard S&P 500 ETF"  # stored facts still serve
    assert detail.quote.price == 685.28  # quote still serves


@pytest.mark.parametrize("bad", ["", "   ", "TOOLONG", "12X", "BR.K"])
def test_detail_rejects_an_invalid_symbol_before_the_lookup(bad):
    use_case, lookup, *_ = _detail_use_case()
    with pytest.raises(ValueError):
        use_case.execute(bad)
    assert lookup.get_calls == []  # rejected at the edge, before touching the repository
