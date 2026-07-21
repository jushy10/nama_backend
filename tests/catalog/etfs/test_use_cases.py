from datetime import datetime, timedelta, timezone

import pytest

from app.stocks.ai.analysis.entities import Confidence, InvestmentAnalysis, Recommendation
from app.stocks.ai.analysis.interfaces import InvestmentAnalysisCacheAdapter
from app.stocks.entities import Quote, StockPerformance
from app.stocks.interfaces import StockPerformanceAdapter, StockQuoteAdapter
from app.stocks.catalog.etfs.entities import (
    EtfCategories,
    EtfHolding,
    EtfProfile,
    EtfScreenIntent,
    EtfSearchPage,
    EtfSearchResult,
    EtfSectorWeight,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.catalog.etfs.interfaces import (
    EtfAnalysisAdapter,
    EtfProfileAdapter,
    EtfScreenerAdapter,
    EtfScreenerQueryAdapter,
)
from app.stocks.catalog.etfs.interfaces import (
    EtfLookupRepositoryAdapter,
    EtfRepositoryAdapter,
    EtfSearchRepositoryAdapter,
    EtfSyncCounts,
)
from app.stocks.catalog.etfs.use_cases import (
    AiScreenEtfs,
    EtfSyncReport,
    GetEtfAnalysis,
    GetEtfDetail,
    ListEtfCategories,
    SearchEtfs,
    SyncEtfs,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


def _etf(ticker, *, net_assets=1e10):
    return ScreenedEtf(ticker=ticker, net_assets=net_assets)


def _a_screen(n: int) -> tuple[ScreenedEtf, ...]:
    return tuple(_etf(f"E{i:04d}", net_assets=1e9 + i) for i in range(n))


class _FakeScreener(EtfScreenerAdapter):
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


_NOT_CALLED = object()  # sentinel: the enrichment work-list query was never issued


class _FakeProfileProvider(EtfProfileAdapter):
    def __init__(self, mapping=None, *, errors=()) -> None:
        self._mapping = dict(mapping or {})
        self._errors = set(errors)
        self.calls: list[str] = []

    def get_profile(self, symbol):
        self.calls.append(symbol)
        if symbol in self._errors:
            raise StockDataUnavailable(symbol, "yahoo blocked")
        return self._mapping.get(symbol, EtfProfile.empty())


class _FakeRepo(EtfRepositoryAdapter):
    def __init__(self, *, counts=EtfSyncCounts(0, 0), targets=()) -> None:
        self._counts = counts
        self._targets = tuple(targets)
        self.upserted: tuple[ScreenedEtf, ...] | None = None
        self.profiled: list[tuple[str, EtfProfile]] = []
        self.refresh_limit: object = _NOT_CALLED

    def upsert_screen(self, etfs):
        self.upserted = tuple(etfs)
        return self._counts

    def profile_refresh_targets(self, limit):
        self.refresh_limit = limit
        return self._targets

    def upsert_profile(self, ticker, profile):
        self.profiled.append((ticker, profile))


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=EtfSyncCounts(added=5, updated=45))

    report = SyncEtfs(screener, repo, _FakeProfileProvider()).execute()

    assert isinstance(report, EtfSyncReport)
    assert screener.calls == 1
    assert screener.min_net_assets == SyncEtfs.MIN_NET_ASSETS  # the AUM floor is threaded through
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 5, 45)
    assert report.skipped is False
    assert (report.enriched, report.enrich_failed) == (0, 0)  # no funds in the work-list
    assert repo.refresh_limit is None  # the enrichment pass still ran (uncapped), just no targets


def test_sync_skips_an_empty_screen_without_touching_the_store():
    repo = _FakeRepo()
    provider = _FakeProfileProvider()

    report = SyncEtfs(_FakeScreener(()), repo, provider).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed) == (0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment pass is skipped too — a blocked bulk screen means blocked .info calls.
    assert repo.refresh_limit is _NOT_CALLED
    assert provider.calls == []


def test_sync_skips_an_implausibly_small_screen():
    repo = _FakeRepo()
    report = SyncEtfs(
        _FakeScreener(_a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN - 1)), repo, _FakeProfileProvider()
    ).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.refresh_limit is _NOT_CALLED  # enrichment not reached


def test_sync_propagates_a_hard_screen_failure():
    repo = _FakeRepo()
    with pytest.raises(StockDataUnavailable):
        SyncEtfs(
            _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked")),
            repo,
            _FakeProfileProvider(),
        ).execute()
    assert repo.upserted is None  # nothing written on a hard failure
    assert repo.refresh_limit is _NOT_CALLED


def test_sync_refreshes_each_stored_funds_profile():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(targets=("SPY", "QQQ"))
    spy = EtfProfile(category="large_blend", fund_family="SSGA")
    qqq = EtfProfile(category="large_growth", fund_family="Invesco")
    provider = _FakeProfileProvider({"SPY": spy, "QQQ": qqq})

    report = SyncEtfs(_FakeScreener(screen), repo, provider).execute()

    assert provider.calls == ["SPY", "QQQ"]
    assert repo.profiled == [("SPY", spy), ("QQQ", qqq)]
    assert (report.enriched, report.enrich_failed) == (2, 0)


def test_enrichment_counts_a_source_failure_and_keeps_going():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(targets=("SPY", "BADX", "QQQ"))
    provider = _FakeProfileProvider(
        {"SPY": EtfProfile(category="large_blend"), "QQQ": EtfProfile(category="large_growth")},
        errors=("BADX",),
    )

    report = SyncEtfs(_FakeScreener(screen), repo, provider).execute()

    # BADX raised, so its stored profile is left untouched — but the sweep continued to QQQ.
    assert [ticker for ticker, _ in repo.profiled] == ["SPY", "QQQ"]
    assert (report.enriched, report.enrich_failed) == (2, 1)


def test_enrichment_persists_even_a_sparse_profile():
    # A reachable-but-sparse fund (empty profile) is still fetched and persisted — the
    # merge-preserving upsert handles the emptiness — so it's counted enriched, not left for later.
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(targets=("SPARSE",))
    provider = _FakeProfileProvider({})  # SPARSE -> EtfProfile.empty()

    report = SyncEtfs(_FakeScreener(screen), repo, provider).execute()

    assert repo.profiled == [("SPARSE", EtfProfile.empty())]  # upsert still called
    assert (report.enriched, report.enrich_failed) == (1, 0)
    # Empty holdings+sectors is the funds_data-blocked signature — surfaced as a health signal.
    assert report.enriched_without_holdings == 1


def test_enrichment_flags_only_funds_missing_holdings_and_sectors():
    # The health counter distinguishes a fund whose funds_data served (holdings/sectors present)
    # from one that came back with neither (the block signature). Both still count as enriched —
    # .info served, so the scalar profile persisted regardless.
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(targets=("FULL", "BARE"))
    provider = _FakeProfileProvider(
        {
            "FULL": EtfProfile(
                category="large_blend",
                top_holdings=(EtfHolding(ticker="NVDA", name="NVIDIA Corp", weight=7.89),),
                sector_weightings=(EtfSectorWeight(sector="technology", weight=39.1),),
            ),
            "BARE": EtfProfile(category="large_growth"),  # .info only — funds_data empty
        }
    )

    report = SyncEtfs(_FakeScreener(screen), repo, provider).execute()

    assert (report.enriched, report.enrich_failed) == (2, 0)
    assert report.enriched_without_holdings == 1  # only BARE is flagged


def test_enrichment_defaults_to_no_limit_then_overrides():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)

    # Default: uncapped — the enrichment pass asks the repo for every stored fund.
    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeProfileProvider()).execute()
    assert repo.refresh_limit is None

    # An explicit limit still caps a run — the throttle escape hatch.
    repo = _FakeRepo()
    SyncEtfs(_FakeScreener(screen), repo, _FakeProfileProvider()).execute(limit=25)
    assert repo.refresh_limit == 25


# --- SearchEtfs / ListEtfCategories (the read side) ----------------------------------------

_RESULT = EtfSearchResult(
    ticker="SPY",
    name="SPDR S&P 500 ETF Trust",
    exchange="NYSE",
    net_assets=5e11,
    expense_ratio=0.09,
    category="large_blend",
)


class _FakeSearchRepo(EtfSearchRepositoryAdapter):
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
        categories=["Large Growth"],
        sort=EtfSort.EXPENSE_RATIO,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "Gold"
    assert c.categories == ("large_growth",)  # slugged to the stored convention
    assert (c.sort, c.direction) == (EtfSort.EXPENSE_RATIO, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_multi_select_categories_slugs_dedupes_and_drops_blanks():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(
        categories=["Large Growth", "large_growth", "", "Large Blend"]
    )
    # Each label slugged; blanks dropped; duplicates collapsed with first-seen order kept.
    assert repo.criteria.categories == ("large_growth", "large_blend")


def test_search_blank_text_and_category_become_empty():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(query="   ", categories=["", "  "])
    c = repo.criteria
    assert c.query is None
    assert c.categories == ()


def test_search_defaults_to_net_assets_desc_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute()
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)
    assert (c.limit, c.offset) == (SearchEtfs.DEFAULT_LIMIT, 0)
    assert c.query is None
    assert c.categories == ()


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


# --- AiScreenEtfs (the AI-driven read side) ------------------------------------------------


class _FakeEtfTranslator(EtfScreenerQueryAdapter):
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result if result is not None else EtfScreenIntent()
        self._error = error
        self.query: str | None = None
        self.categories: tuple[str, ...] | None = None

    def translate(self, query, *, categories):
        self.query = query
        self.categories = tuple(categories)
        if self._error is not None:
            raise self._error
        return self._result


def test_ai_screen_translates_with_the_allowed_category_vocabulary():
    repo = _FakeSearchRepo(categories=EtfCategories(("large_blend", "large_growth")))
    intent = EtfScreenIntent(categories=("large_growth",), sort=EtfSort.NET_ASSETS)
    translator = _FakeEtfTranslator(result=intent)

    result = AiScreenEtfs(translator, repo).execute(query="  big growth funds ")

    # The intent is returned as-is; the request is trimmed and the stored categories are handed to
    # the translator as its allowed vocabulary.
    assert result is intent
    assert translator.query == "big growth funds"
    assert translator.categories == ("large_blend", "large_growth")
    assert repo.categories_calls == 1


def test_ai_screen_requires_a_query():
    repo = _FakeSearchRepo()
    with pytest.raises(ValueError):
        AiScreenEtfs(_FakeEtfTranslator(), repo).execute(query="   ")


def test_ai_screen_propagates_a_translation_failure():
    repo = _FakeSearchRepo(categories=EtfCategories(("large_blend",)))
    translator = _FakeEtfTranslator(error=StockDataUnavailable("q", "model down"))
    with pytest.raises(StockDataUnavailable):
        AiScreenEtfs(translator, repo).execute(query="tech funds")
    assert repo.categories_calls == 1


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
    # The DB-read stored profile: no trailing returns (they're no longer stored — the detail
    # overlays them live for the performance block; see _live_returns_profile below).
    return EtfProfile(
        fund_family="Vanguard",
        net_assets=1.8e12,  # a wrong-answer sentinel: the table's net_assets must win
        expense_ratio=0.05,  # sentinel: the table's expense_ratio must win
        nav=685.28,
        dividend_yield=1.03,
        description="An S&P 500 index fund.",
        top_holdings=(EtfHolding(ticker="NVDA", name="NVIDIA Corp", weight=7.89),),
        sector_weightings=(EtfSectorWeight(sector="technology", weight=39.13),),
    )


def _live_returns_profile() -> EtfProfile:
    # What the live Yahoo profile read carries for the performance block's return ladder — only
    # the returns matter here (the rest of the profile is DB-read, not taken from this).
    return EtfProfile(ytd_return=11.25, three_year_return=20.41, five_year_return=13.01)


class _FakeLookup(EtfLookupRepositoryAdapter):
    def __init__(self, facts: EtfSearchResult | None, profile: EtfProfile | None = None) -> None:
        self._facts = facts
        self._profile = profile if profile is not None else EtfProfile.empty()
        self.get_calls: list[str] = []
        self.profile_calls: list[str] = []

    def is_etf(self, ticker: str) -> bool:
        return self._facts is not None

    def get(self, ticker: str) -> EtfSearchResult | None:
        self.get_calls.append(ticker)
        return self._facts

    def get_stored_profile(self, ticker: str) -> EtfProfile:
        self.profile_calls.append(ticker)
        return self._profile


class _FakeQuotes(StockQuoteAdapter):
    def __init__(self, quote: Quote | None = None, error: Exception | None = None) -> None:
        self._quote = quote
        self._error = error
        self.calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._quote or _quote(symbol)


def _a_performance() -> StockPerformance:
    return StockPerformance(
        one_week=1.1, one_month=2.2, three_month=3.3, six_month=4.4, ytd=5.5, one_year=6.6
    )


class _FakePerformance(StockPerformanceAdapter):
    def __init__(
        self, perf: StockPerformance | None = None, error: Exception | None = None
    ) -> None:
        self._perf = perf
        self._error = error
        self.calls: list[str] = []

    def get_performance(self, symbol: str) -> StockPerformance:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._perf if self._perf is not None else _a_performance()


_UNSET = object()  # sentinel so an explicit facts=None (non-ETF) differs from "not passed"


def _detail_use_case(
    *,
    facts=_UNSET,
    quote=None,
    quote_error=None,
    profile=None,
    performance=None,
    performance_error=None,
    live_returns=None,  # the live Yahoo profile the returns are overlaid from (None -> empty)
    live_returns_error=False,  # simulate a blocked live Yahoo read (raises StockDataUnavailable)
):
    # The stored profile is read from the lookup repo; the performance provider backs the opt-in
    # 'performance' block's Alpaca windows; the profile provider backs that same block's live 3y/5y
    # returns (no longer stored). Returns (use_case, lookup, quotes, perf, profile_provider).
    lookup = _FakeLookup(_facts() if facts is _UNSET else facts, profile)
    quotes = _FakeQuotes(quote, quote_error)
    perf = _FakePerformance(performance, performance_error)
    mapping = {} if live_returns is None else {"VOO": live_returns}
    profile_provider = _FakeProfileProvider(
        mapping, errors=("VOO",) if live_returns_error else ()
    )
    use_case = GetEtfDetail(lookup, quotes, perf, profile_provider)
    return use_case, lookup, quotes, perf, profile_provider


def test_detail_assembles_quote_stored_facts_and_profile():
    use_case, lookup, quotes, perf, _ = _detail_use_case(quote=_quote(), profile=_a_profile())

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
    # Stored profile enrichment rides along, read from the DB (not a live Yahoo call).
    assert detail.profile.fund_family == "Vanguard"
    assert detail.profile.top_holdings[0].ticker == "NVDA"
    assert quotes.calls == ["VOO"]
    assert lookup.profile_calls == ["VOO"]  # the stored profile is read regardless of the includes
    # No includes requested: no performance call, and the block is null.
    assert detail.include == frozenset()
    assert detail.performance is None
    assert perf.calls == []


def test_detail_falls_back_to_profile_figures_when_the_table_lacks_them():
    # A fund the table has no net_assets/expense_ratio for yet: the profile fills the gap (the
    # assemble precedence — facts first, profile second).
    use_case, *_ = _detail_use_case(
        facts=_facts(net_assets=None, expense_ratio=None),
        profile=_a_profile(),  # carries net_assets=1.8e12, expense_ratio=0.05
    )

    detail = use_case.execute("VOO")

    assert detail.net_assets == 1.8e12
    assert detail.expense_ratio == 0.05


def test_detail_404s_before_any_upstream_call_for_a_non_etf():
    use_case, lookup, quotes, perf, provider = _detail_use_case(
        facts=None, performance=_a_performance(), live_returns=_live_returns_profile()
    )  # not in the universe

    with pytest.raises(StockNotFound):
        use_case.execute("AAPL", include=["performance"])

    # The membership gate short-circuits: neither the quote, the stored profile, the (requested)
    # performance, nor the live returns read was reached.
    assert lookup.get_calls == ["AAPL"]
    assert quotes.calls == []
    assert lookup.profile_calls == []
    assert perf.calls == []
    assert provider.calls == []


def test_detail_propagates_a_quote_failure():
    # The quote is primary — its failure propagates (mapped to 502 at the edge), not degraded.
    use_case, lookup, _, perf, provider = _detail_use_case(
        quote_error=StockDataUnavailable("VOO", "alpaca down"),
        performance=_a_performance(),
        live_returns=_live_returns_profile(),
    )

    with pytest.raises(StockDataUnavailable):
        use_case.execute("VOO", include=["performance"])

    # Neither the stored profile, the (requested) performance, nor the live returns read is reached
    # once the primary has failed.
    assert lookup.profile_calls == []
    assert perf.calls == []
    assert provider.calls == []


def test_detail_serves_an_empty_profile_for_an_unenriched_fund():
    # A fund the sync hasn't profile-enriched yet: the stored profile is empty, but the card still
    # serves the quote + stored facts on a 200-worthy result.
    use_case, *_ = _detail_use_case(quote=_quote())  # profile defaults to EtfProfile.empty()

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


def test_detail_records_requested_includes_without_fetching_performance():
    # metrics/dividends draw from the DB-read profile, so requesting them costs no extra call —
    # only the recorded include set changes; the performance block's own calls are untouched.
    use_case, lookup, _, perf, provider = _detail_use_case(
        profile=_a_profile(), live_returns=_live_returns_profile()
    )

    detail = use_case.execute("VOO", include=["metrics", "dividends"])

    assert detail.include == frozenset({"metrics", "dividends"})
    assert lookup.profile_calls == ["VOO"]  # the stored profile was read (metrics' NAV + yield)
    assert perf.calls == []  # but performance was not requested, so no Alpaca windows call
    assert provider.calls == []  # and no live Yahoo returns read (that rides the performance block)
    assert detail.performance is None


def test_detail_fetches_performance_only_when_requested():
    perf_data = _a_performance()
    use_case, _, _, perf, _ = _detail_use_case(
        profile=_a_profile(), performance=perf_data
    )

    detail = use_case.execute("VOO", include=["performance"])

    assert detail.include == frozenset({"performance"})
    assert perf.calls == ["VOO"]
    assert detail.performance == perf_data  # the trailing windows the block serves


def test_detail_overlays_live_returns_onto_the_profile_for_the_performance_block():
    # The 3y/5y returns are no longer stored: when performance is requested, they're fetched live
    # from Yahoo and overlaid onto the (otherwise DB-read) profile the block surfaces.
    use_case, _, _, _, provider = _detail_use_case(
        profile=_a_profile(),
        performance=_a_performance(),
        live_returns=_live_returns_profile(),
    )

    detail = use_case.execute("VOO", include=["performance"])

    assert provider.calls == ["VOO"]  # the live Yahoo read was made
    assert detail.profile.three_year_return == 20.41
    assert detail.profile.five_year_return == 13.01
    # The overlay preserves the DB-read half of the profile (only the return ladder changes).
    assert detail.profile.fund_family == "Vanguard"
    assert detail.profile.top_holdings[0].ticker == "NVDA"


def test_detail_does_not_fetch_live_returns_without_the_performance_block():
    # No performance block -> no live Yahoo call, and the (unstored) returns stay null.
    use_case, _, _, _, provider = _detail_use_case(
        profile=_a_profile(), live_returns=_live_returns_profile()
    )

    detail = use_case.execute("VOO", include=["metrics"])

    assert provider.calls == []  # the returns are only fetched for the performance block
    assert detail.profile.three_year_return is None
    assert detail.profile.five_year_return is None


def test_detail_live_returns_degrade_to_none_when_yahoo_is_blocked():
    # Best-effort even when requested: a blocked live Yahoo read leaves the returns null (the
    # profile keeps its DB values) without sinking the card — mirrors the Alpaca windows beside it.
    perf_data = _a_performance()
    use_case, _, _, _, provider = _detail_use_case(
        quote=_quote(),
        profile=_a_profile(),
        performance=perf_data,
        live_returns_error=True,
    )

    detail = use_case.execute("VOO", include=["performance"])

    assert provider.calls == ["VOO"]  # attempted...
    assert detail.profile.three_year_return is None  # ...but blocked -> null
    assert detail.profile.five_year_return is None
    assert detail.performance == perf_data  # the Alpaca windows still serve
    assert detail.quote.price == 685.28  # the card still serves


def test_detail_accepts_comma_separated_includes():
    use_case, *_ = _detail_use_case(profile=_a_profile())
    detail = use_case.execute("VOO", include=["metrics,performance"])
    assert detail.include == frozenset({"metrics", "performance"})


def test_detail_performance_degrades_to_none_when_the_windows_read_fails():
    # Best-effort even when requested: a blocked Alpaca performance read leaves the block's gains
    # null (performance=None) rather than sinking the card — the quote + facts still serve.
    use_case, *_ = _detail_use_case(
        quote=_quote(),
        performance_error=StockDataUnavailable("VOO", "alpaca windows down"),
    )

    detail = use_case.execute("VOO", include=["performance"])

    assert "performance" in detail.include  # asked for...
    assert detail.performance is None  # ...but the best-effort read failed
    assert detail.quote.price == 685.28  # the card still serves


def test_detail_rejects_an_unknown_include_before_the_lookup():
    use_case, lookup, quotes, *_ = _detail_use_case()
    with pytest.raises(ValueError):
        use_case.execute("VOO", include=["bogus"])
    # Rejected at the edge (normalization), before the membership gate or any upstream call.
    assert lookup.get_calls == []
    assert quotes.calls == []


# --- GetEtfAnalysis --------------------------------------------------------------------------
#
# Composes GetEtfDetail (the primary snapshot) + an EtfAnalysisAdapter (the AI read). The detail's
# normalization / membership gate / quote-primary failures all propagate unchanged; the analyzer is
# only reached once a snapshot is in hand.


class _FakeEtfAnalysisProvider(EtfAnalysisAdapter):
    def __init__(self, analysis: InvestmentAnalysis | None = None, *, raises=None) -> None:
        self._analysis = analysis
        self._raises = raises
        self.received: list = []

    def analyze(self, detail):
        self.received.append(detail)
        if self._raises is not None:
            raise self._raises
        assert self._analysis is not None
        return self._analysis


def _an_analysis(**overrides) -> InvestmentAnalysis:
    base = dict(
        symbol="VOO",
        recommendation=Recommendation.BUY,
        confidence=Confidence.HIGH,
        thesis="A cheap, broad way to own the whole market.",
        strengths=("Very low yearly cost",),
        risks=("Concentrated in a few big tech names",),
        model="claude-haiku-4-5",
        generated_at=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return InvestmentAnalysis(**base)


def test_analysis_builds_the_snapshot_and_returns_the_model_read():
    # The real GetEtfDetail assembles the snapshot; the fake analyzer records what it was handed.
    detail_uc, lookup, quotes, perf, _ = _detail_use_case(
        quote=_quote(),
        profile=_a_profile(),
        live_returns=_live_returns_profile(),  # overlaid onto the perf snapshot's return ladder
    )
    analyzer = _FakeEtfAnalysisProvider(_an_analysis())

    analysis = GetEtfAnalysis(detail_uc, analyzer).execute("voo")  # lower-case in -> normalized

    assert analysis.recommendation is Recommendation.BUY
    assert len(analyzer.received) == 1
    detail = analyzer.received[0]
    assert detail.ticker == "VOO"
    assert detail.expense_ratio == 0.03  # stored fact (table wins over the profile)
    # The performance snapshot is fetched for the analysis (the Alpaca windows + the live 3y/5y
    # overlay), so the model sees the returns — the richest context the card can build.
    assert perf.calls == ["VOO"]
    assert detail.performance is not None
    assert detail.profile.three_year_return == 20.41


def test_analysis_rejects_a_bad_symbol_before_any_work():
    detail_uc, lookup, quotes, *_ = _detail_use_case(quote=_quote())
    analyzer = _FakeEtfAnalysisProvider(_an_analysis())

    with pytest.raises(ValueError):
        GetEtfAnalysis(detail_uc, analyzer).execute("TOOLONG")

    assert lookup.get_calls == []  # gated at normalization, before the membership lookup
    assert analyzer.received == []


def test_analysis_404s_for_a_non_etf_before_the_analyzer():
    detail_uc, lookup, quotes, *_ = _detail_use_case(facts=None)  # not in the ETF universe
    analyzer = _FakeEtfAnalysisProvider(_an_analysis())

    with pytest.raises(StockNotFound):
        GetEtfAnalysis(detail_uc, analyzer).execute("XYZ")

    assert quotes.calls == []  # membership gate is before the (primary) quote
    assert analyzer.received == []


def test_analysis_propagates_a_quote_failure_before_the_analyzer():
    detail_uc, *_ = _detail_use_case(
        quote_error=StockDataUnavailable("VOO", "alpaca down")
    )
    analyzer = _FakeEtfAnalysisProvider(_an_analysis())

    with pytest.raises(StockDataUnavailable):
        GetEtfAnalysis(detail_uc, analyzer).execute("VOO")

    assert analyzer.received == []  # the primary snapshot failed, so the model was never asked


def test_analysis_propagates_a_model_failure():
    detail_uc, *_ = _detail_use_case(quote=_quote(), profile=_a_profile())
    analyzer = _FakeEtfAnalysisProvider(raises=StockDataUnavailable("VOO", "bedrock down"))

    with pytest.raises(StockDataUnavailable):
        GetEtfAnalysis(detail_uc, analyzer).execute("VOO")


class _FakeAnalysisCache(InvestmentAnalysisCacheAdapter):
    def __init__(self, stored: InvestmentAnalysis | None = None) -> None:
        self._store = {stored.symbol: stored} if stored is not None else {}
        self.puts: list[InvestmentAnalysis] = []

    def get(self, symbol: str) -> InvestmentAnalysis | None:
        return self._store.get(symbol)

    def put(self, analysis: InvestmentAnalysis) -> None:
        self.puts.append(analysis)
        self._store[analysis.symbol] = analysis


def test_analysis_fresh_cache_hit_skips_the_snapshot_and_model():
    # A fresh stored read is served without building the (expensive) snapshot — the live quote +
    # 3y/5y returns — or calling the model. This is the whole point of the cache.
    fresh = _an_analysis(generated_at=datetime.now(timezone.utc))
    detail_uc, lookup, quotes, perf, _ = _detail_use_case(quote=_quote(), profile=_a_profile())
    analyzer = _FakeEtfAnalysisProvider(_an_analysis(thesis="regenerated"))
    cache = _FakeAnalysisCache(stored=fresh)

    result = GetEtfAnalysis(detail_uc, analyzer, cache=cache).execute("voo")

    assert result is fresh
    assert lookup.get_calls == []  # snapshot never built
    assert quotes.calls == []
    assert analyzer.received == []  # model never called
    assert cache.puts == []  # nothing re-stored


def test_analysis_stale_cache_is_regenerated_and_stored():
    stale = _an_analysis(generated_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    detail_uc, *_ = _detail_use_case(quote=_quote(), profile=_a_profile())
    generated = _an_analysis(thesis="a fresh take")
    analyzer = _FakeEtfAnalysisProvider(generated)
    cache = _FakeAnalysisCache(stored=stale)

    result = GetEtfAnalysis(
        detail_uc, analyzer, cache=cache, cache_ttl=timedelta(minutes=30)
    ).execute("VOO")

    assert result is generated  # regenerated, not the stale read
    assert len(analyzer.received) == 1
    assert cache.puts == [generated]  # stored for the next viewer


def test_analysis_cache_miss_generates_and_stores():
    detail_uc, *_ = _detail_use_case(quote=_quote(), profile=_a_profile())
    generated = _an_analysis()
    analyzer = _FakeEtfAnalysisProvider(generated)
    cache = _FakeAnalysisCache()  # empty

    result = GetEtfAnalysis(detail_uc, analyzer, cache=cache).execute("VOO")

    assert result is generated
    assert cache.puts == [generated]


def test_analysis_incomplete_read_is_not_cached():
    # A read missing its bullet lists is returned but never frozen in the cache, so
    # the next view regenerates instead of serving empty strengths/risks for the TTL.
    detail_uc, *_ = _detail_use_case(quote=_quote(), profile=_a_profile())
    incomplete = _an_analysis(strengths=(), risks=())
    analyzer = _FakeEtfAnalysisProvider(incomplete)
    cache = _FakeAnalysisCache()  # empty

    result = GetEtfAnalysis(detail_uc, analyzer, cache=cache).execute("VOO")

    assert result is incomplete  # still returned to the caller
    assert cache.puts == []  # but not stored
