from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import etf_endpoints as endpoints
from app.stocks.ai.analysis.entities import Confidence, InvestmentAnalysis, Recommendation
from app.stocks.entities import Quote, StockPerformance
from app.stocks.catalog.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfHolding,
    EtfProfile,
    EtfScreenIntent,
    EtfSearchPage,
    EtfSearchResult,
    EtfSectorWeight,
    EtfSort,
    SortDirection,
)
from app.stocks.catalog.etfs.repository import EtfSearchRepository
from app.stocks.catalog.etfs.use_cases import ListEtfCategories, SearchEtfs
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


class _FakeSearchRepo(EtfSearchRepository):
    def __init__(self, results=(), categories=()) -> None:
        self._results = tuple(results)
        self._categories = tuple(categories)
        self.criteria = None

    def search(self, criteria):
        self.criteria = criteria
        return EtfSearchPage(
            results=self._results,
            total=len(self._results),
            limit=criteria.limit,
            offset=criteria.offset,
        )

    def categories(self):
        return EtfCategories(self._categories)


def _result(ticker, **kw) -> EtfSearchResult:
    base = dict(
        name=None, exchange=None, net_assets=1e11, expense_ratio=0.1, category=None
    )
    base.update(kw)
    return EtfSearchResult(ticker=ticker, **base)


def _client(repo: _FakeSearchRepo) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_search_use_case] = lambda: SearchEtfs(repo)
    app.dependency_overrides[endpoints.get_categories_use_case] = lambda: ListEtfCategories(repo)
    return TestClient(app)


def test_returns_a_page_envelope_with_rows():
    repo = _FakeSearchRepo(
        [
            _result(
                "SPY",
                name="SPDR S&P 500 ETF Trust",
                exchange="NYSE",
                net_assets=5e11,
                expense_ratio=0.09,
                category="large_blend",
                dividend_yield=1.24,
            )
        ]
    )
    resp = _client(repo).get("/stocks/etfs")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["total"], body["count"], body["limit"], body["offset"]) == (
        1,
        1,
        SearchEtfs.DEFAULT_LIMIT,
        0,
    )
    (row,) = body["results"]
    assert row == {
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "exchange": "NYSE",
        "net_assets": 5e11,
        "expense_ratio": 0.09,
        "category": "large_blend",
        "dividend_yield": 1.24,
    }


def test_passes_query_category_sort_and_paging_to_the_use_case():
    repo = _FakeSearchRepo()
    resp = _client(repo).get(
        "/stocks/etfs?q=gold&category=large_growth&sort=dividend_yield&order=asc&limit=5&offset=10"
    )
    assert resp.status_code == 200
    c = repo.criteria
    assert c.query == "gold"
    # A single category arrives as a one-element list the use case slugs into a tuple.
    assert c.categories == ("large_growth",)
    assert (c.sort, c.direction) == (EtfSort.DIVIDEND_YIELD, SortDirection.ASC)
    assert (c.limit, c.offset) == (5, 10)


def test_passes_repeated_categories_through_as_a_tuple():
    # The category axis repeats — several fund categories at once, ORed together.
    repo = _FakeSearchRepo()
    resp = _client(repo).get(
        "/stocks/etfs?category=large_growth&category=large_blend"
    )
    assert resp.status_code == 200
    assert repo.criteria.categories == ("large_growth", "large_blend")


def test_defaults_sort_to_net_assets_desc():
    repo = _FakeSearchRepo()
    _client(repo).get("/stocks/etfs")
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)
    assert c.categories == ()


def test_rejects_an_unknown_sort():
    repo = _FakeSearchRepo()
    # An out-of-enum sort is a 422 from the query binding, before the use case runs.
    assert _client(repo).get("/stocks/etfs?sort=bogus").status_code == 422


def test_sets_a_short_cache_header():
    repo = _FakeSearchRepo()
    resp = _client(repo).get("/stocks/etfs")
    assert resp.headers["cache-control"] == "public, max-age=60"


def test_categories_endpoint_returns_the_slugs():
    repo = _FakeSearchRepo(
        categories=("commodities_focused", "large_blend", "large_growth")
    )
    resp = _client(repo).get("/stocks/etfs/categories")
    assert resp.status_code == 200
    assert resp.json() == {
        "categories": ["commodities_focused", "large_blend", "large_growth"]
    }
    assert resp.headers["cache-control"] == "public, max-age=300"


# --- GET /stocks/etfs/ai-search (the AI-driven screen) ------------------------------------


class _FakeAiScreen:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result if result is not None else EtfScreenIntent()
        self._error = error
        self.kwargs: dict | None = None

    def execute(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _ai_client(use_case) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ai_etf_search_use_case] = lambda: use_case
    return TestClient(app)


def test_ai_search_returns_the_interpreted_filters_only():
    intent = EtfScreenIntent(
        categories=("large_blend",),
        sort=EtfSort.EXPENSE_RATIO,
        direction=SortDirection.ASC,
        limit=10,
    )
    resp = _ai_client(_FakeAiScreen(result=intent)).get(
        "/stocks/etfs/ai-search", params={"q": "cheap index funds"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "interpreted": {
            "query": None,
            "categories": ["large_blend"],
            "sort": "expense_ratio",
            "direction": "asc",
            "limit": 10,
        }
    }
    # The endpoint returns only the interpretation — no result page.
    assert "results" not in body


def test_ai_search_passes_the_query_through():
    fake = _FakeAiScreen(result=EtfScreenIntent())
    resp = _ai_client(fake).get(
        "/stocks/etfs/ai-search", params={"q": "  top 5 gold funds "}
    )
    assert resp.status_code == 200
    assert fake.kwargs == {"query": "  top 5 gold funds "}


def test_ai_search_requires_a_query():
    # Missing q -> 422 (the param is required).
    resp = _ai_client(_FakeAiScreen()).get("/stocks/etfs/ai-search")
    assert resp.status_code == 422


def test_ai_search_blank_query_is_a_400():
    fake = _FakeAiScreen(error=ValueError("A search request is required."))
    resp = _ai_client(fake).get("/stocks/etfs/ai-search", params={"q": "x"})
    assert resp.status_code == 400


def test_ai_search_translation_failure_is_a_502():
    fake = _FakeAiScreen(error=StockDataUnavailable("q", "model down"))
    resp = _ai_client(fake).get("/stocks/etfs/ai-search", params={"q": "funds"})
    assert resp.status_code == 502


def test_ai_search_sets_a_short_cache_header():
    fake = _FakeAiScreen(result=EtfScreenIntent())
    resp = _ai_client(fake).get("/stocks/etfs/ai-search", params={"q": "funds"})
    assert resp.headers["cache-control"] == "public, max-age=60"


# --- GET /stocks/etf/{ticker} (the detail card) -------------------------------------------


class _FakeDetailUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []
        self.includes: list = []

    def execute(self, ticker: str, include=None) -> EtfDetail:
        self.calls.append(ticker)
        self.includes.append(include)
        if self._error is not None:
            raise self._error
        return self._result


def _detail_client(fake: _FakeDetailUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_etf_detail_use_case] = lambda: fake
    return TestClient(app)


def _a_performance() -> StockPerformance:
    return StockPerformance(
        one_week=0.5,
        one_month=1.2,
        three_month=3.4,
        six_month=6.5,
        ytd=8.9,
        one_year=12.3,
    )


def _a_detail(
    *,
    profile: EtfProfile | None = None,
    include=frozenset(),
    performance: StockPerformance | None = None,
) -> EtfDetail:
    quote = Quote(
        symbol="VOO",
        price=685.28,
        previous_close=682.07,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
    )
    facts = EtfSearchResult(
        ticker="VOO",
        name="Vanguard S&P 500 ETF",
        exchange="NYSE",
        net_assets=1_701_513_003_008.0,
        expense_ratio=0.03,
        category="large_blend",
    )
    if profile is None:
        profile = EtfProfile(
            fund_family="Vanguard",
            nav=685.28,
            dividend_yield=1.03,
            ytd_return=11.25,
            three_year_return=20.41,
            five_year_return=13.01,
            description="The fund employs an indexing investment approach.",
            top_holdings=(EtfHolding(ticker="NVDA", name="NVIDIA Corp", weight=7.89),),
            sector_weightings=(EtfSectorWeight(sector="technology", weight=39.13),),
        )
    return EtfDetail.assemble(
        "VOO", quote, facts, profile, include=frozenset(include), performance=performance
    )


def test_detail_returns_the_full_json_shape_with_all_includes():
    # Every opt-in block requested: the base card + the always-on enrichment + the three nested
    # blocks (metrics / dividends / performance). Note ytd rides the performance block's Alpaca
    # window (8.9) — Yahoo's own ytd_return is deliberately no longer surfaced.
    fake = _FakeDetailUseCase(
        result=_a_detail(
            include={"metrics", "dividends", "performance"}, performance=_a_performance()
        )
    )
    resp = _detail_client(fake).get(
        "/stocks/etf/VOO?include=metrics,dividends,performance"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ticker": "VOO",
        "name": "Vanguard S&P 500 ETF",
        "exchange": "NYSE",
        "asset_type": "etf",
        "price": 685.28,
        "change": 3.21,  # 685.28 - 682.07, the quote's derived move
        "change_percent": 0.47,  # vs the previous close
        "previous_close": 682.07,
        "as_of": "2026-07-06T20:00:00Z",
        "category": "large_blend",
        # Always-on Yahoo enrichment.
        "fund_family": "Vanguard",
        "description": "The fund employs an indexing investment approach.",
        "top_holdings": [{"ticker": "NVDA", "name": "NVIDIA Corp", "weight": 7.89}],
        "sector_weightings": [{"sector": "technology", "weight": 39.13}],
        # Opt-in blocks.
        "metrics": {
            "expense_ratio": 0.03,
            "nav": 685.28,
            "net_assets": 1_701_513_003_008.0,
        },
        "dividends": {"yield_percentage": 1.03},
        "performance": {
            "1w": 0.5,
            "1m": 1.2,
            "3m": 3.4,
            "6m": 6.5,
            "ytd": 8.9,  # the Alpaca window, not Yahoo's ytd_return
            "1y": 12.3,
            "3y": 20.41,  # Yahoo annualized
            "5y": 13.01,
        },
    }
    assert fake.calls == ["VOO"]


def test_detail_omits_unrequested_blocks():
    # No includes: the base card + always-on enrichment serve, and all three blocks are null.
    fake = _FakeDetailUseCase(result=_a_detail())  # include defaults to frozenset()
    resp = _detail_client(fake).get("/stocks/etf/VOO")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Vanguard S&P 500 ETF"  # base + enrichment present
    assert body["fund_family"] == "Vanguard"
    assert body["metrics"] is None
    assert body["dividends"] is None
    assert body["performance"] is None


def test_detail_serves_null_enrichment_for_an_unenriched_fund():
    # A fund the sync hasn't profile-enriched yet has an empty stored profile — the quote + stored
    # facts still serve on a 200, with the always-on enrichment fields null/empty.
    fake = _FakeDetailUseCase(result=_a_detail(profile=EtfProfile.empty()))
    resp = _detail_client(fake).get("/stocks/etf/VOO")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == 685.28  # quote still serves
    assert body["name"] == "Vanguard S&P 500 ETF"  # stored facts still serve
    assert body["fund_family"] is None
    assert body["description"] is None
    assert body["top_holdings"] == []
    assert body["sector_weightings"] == []


def test_detail_emits_only_the_requested_block():
    fake = _FakeDetailUseCase(result=_a_detail(include={"metrics"}))
    resp = _detail_client(fake).get("/stocks/etf/VOO?include=metrics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metrics"] == {
        "expense_ratio": 0.03,
        "nav": 685.28,
        "net_assets": 1_701_513_003_008.0,
    }
    assert body["dividends"] is None
    assert body["performance"] is None


def test_detail_passes_include_through_to_the_use_case():
    # Repeated params reach the use case as a list; the use case owns the split/validation.
    fake = _FakeDetailUseCase(result=_a_detail())
    _detail_client(fake).get("/stocks/etf/VOO?include=metrics&include=performance")
    assert fake.includes == [["metrics", "performance"]]


def test_detail_unknown_include_is_a_400():
    # The use case raises ValueError on an unknown include; the endpoint maps it to 400.
    fake = _FakeDetailUseCase(error=ValueError("Unknown include(s): bogus."))
    resp = _detail_client(fake).get("/stocks/etf/VOO?include=bogus")
    assert resp.status_code == 400


def test_detail_serves_null_within_the_blocks_when_the_profile_is_empty():
    # Best-effort: a blocked Yahoo read leaves the profile empty — the quote + stored facts still
    # serve on a 200. The always-on enrichment goes null / []; a requested block is still emitted
    # but its Yahoo-sourced figures are null, while the stored facts still fill metrics.
    fake = _FakeDetailUseCase(
        result=_a_detail(
            profile=EtfProfile.empty(),
            include={"metrics", "dividends", "performance"},
            performance=None,  # the best-effort Alpaca windows read was blocked too
        )
    )
    resp = _detail_client(fake).get(
        "/stocks/etf/VOO?include=metrics,dividends,performance"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == 685.28  # quote still serves
    assert body["name"] == "Vanguard S&P 500 ETF"  # stored facts still serve
    # Always-on enrichment degrades to null / [].
    assert body["fund_family"] is None
    assert body["description"] is None
    assert body["top_holdings"] == []
    assert body["sector_weightings"] == []
    # The requested blocks are emitted; the stored facts fill metrics, the rest is null.
    assert body["metrics"] == {
        "expense_ratio": 0.03,
        "nav": None,
        "net_assets": 1_701_513_003_008.0,
    }
    assert body["dividends"] == {"yield_percentage": None}
    assert body["performance"] == {
        "1w": None,
        "1m": None,
        "3m": None,
        "6m": None,
        "ytd": None,
        "1y": None,
        "3y": None,
        "5y": None,
    }


def test_detail_sets_the_cache_header():
    fake = _FakeDetailUseCase(result=_a_detail())
    resp = _detail_client(fake).get("/stocks/etf/VOO")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_detail_non_etf_is_a_404():
    # Not in the stored ETF universe -> "not an ETF".
    fake = _FakeDetailUseCase(error=StockNotFound("AAPL"))
    resp = _detail_client(fake).get("/stocks/etf/AAPL")
    assert resp.status_code == 404


def test_detail_invalid_symbol_is_a_400():
    fake = _FakeDetailUseCase(error=ValueError("'123' is not a valid ETF symbol."))
    resp = _detail_client(fake).get("/stocks/etf/123")
    assert resp.status_code == 400


def test_detail_quote_failure_is_a_502():
    # The quote is primary — its failure surfaces as the same 502 the quote/ticker endpoints use.
    fake = _FakeDetailUseCase(error=StockDataUnavailable("VOO", "alpaca down"))
    resp = _detail_client(fake).get("/stocks/etf/VOO")
    assert resp.status_code == 502


# --- GET /stocks/etf/{ticker}/analysis (the AI read) --------------------------------------------


class _FakeEtfAnalysisUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, ticker: str) -> InvestmentAnalysis:
        self.calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._result


def _analysis_client(fake: _FakeEtfAnalysisUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_etf_analysis_use_case] = lambda: fake
    return TestClient(app)


def _an_analysis(**overrides) -> InvestmentAnalysis:
    base = dict(
        symbol="VOO",
        recommendation=Recommendation.BUY,
        confidence=Confidence.HIGH,
        thesis="A cheap, broad way to own the whole market.",
        strengths=("Very low yearly cost", "Broadly diversified"),
        risks=("Concentrated in a few big tech names",),
        model="claude-haiku-4-5",
        generated_at=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return InvestmentAnalysis(**base)


def test_analysis_returns_the_full_json_shape():
    fake = _FakeEtfAnalysisUseCase(result=_an_analysis())
    resp = _analysis_client(fake).get("/stocks/etf/voo/analysis")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The entity's `symbol` is presented as `ticker`; an asset_type marker rides along.
    assert body["ticker"] == "VOO"
    assert body["asset_type"] == "etf"
    assert body["recommendation"] == "buy"  # enum -> its string value
    assert body["confidence"] == "high"
    assert body["thesis"].startswith("A cheap")
    assert body["strengths"] == ["Very low yearly cost", "Broadly diversified"]
    assert body["risks"] == ["Concentrated in a few big tech names"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["generated_at"] == "2026-07-06T20:00:00Z"
    # The disclaimer is authored by the service (not the model) and always attached.
    assert "not financial" in body["disclaimer"].lower()
    assert fake.calls == ["voo"]  # the raw path param reaches the use case (it normalizes)


def test_analysis_sets_the_cache_header():
    fake = _FakeEtfAnalysisUseCase(result=_an_analysis())
    resp = _analysis_client(fake).get("/stocks/etf/VOO/analysis")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_analysis_non_etf_is_a_404():
    fake = _FakeEtfAnalysisUseCase(error=StockNotFound("AAPL"))
    resp = _analysis_client(fake).get("/stocks/etf/AAPL/analysis")
    assert resp.status_code == 404


def test_analysis_invalid_symbol_is_a_400():
    fake = _FakeEtfAnalysisUseCase(error=ValueError("'123' is not a valid ETF symbol."))
    resp = _analysis_client(fake).get("/stocks/etf/123/analysis")
    assert resp.status_code == 400


def test_analysis_model_or_quote_failure_is_a_502():
    # Both the primary snapshot (the quote) and the model call surface as StockDataUnavailable -> 502.
    fake = _FakeEtfAnalysisUseCase(error=StockDataUnavailable("VOO", "bedrock down"))
    resp = _analysis_client(fake).get("/stocks/etf/VOO/analysis")
    assert resp.status_code == 502
