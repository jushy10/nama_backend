"""Tests for the ETF read endpoints (GET /stocks/etfs, GET /stocks/etfs/categories, and
GET /stocks/etf/{ticker}).

Offline: the use cases are built over in-memory fakes and injected through dependency_overrides, so
this checks the controllers + presenters + query binding — the search response envelope, the
q/category/sort/order/paging params reaching the use case, the enum validation, the categories
menu, and for the detail card: the JSON shape (quote + stored facts + best-effort profile), the
404-for-non-ETF, the 502-on-quote-failure, the best-effort degradation, and the cache header — with
no database, Alpaca, or Yahoo.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import etf_endpoints as endpoints
from app.stocks.entities import Quote, StockPerformance
from app.stocks.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfHolding,
    EtfProfile,
    EtfSearchPage,
    EtfSearchResult,
    EtfSectorWeight,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.repository import EtfSearchRepository
from app.stocks.etfs.use_cases import ListEtfCategories, SearchEtfs
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


class _FakeSearchRepo(EtfSearchRepository):
    """Records the criteria it was handed and returns a page built from canned rows / categories."""

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
    }


def test_passes_query_category_sort_and_paging_to_the_use_case():
    repo = _FakeSearchRepo()
    resp = _client(repo).get(
        "/stocks/etfs?q=gold&category=large_growth&sort=expense_ratio&order=asc&limit=5&offset=10"
    )
    assert resp.status_code == 200
    c = repo.criteria
    assert c.query == "gold"
    assert c.category == "large_growth"
    assert (c.sort, c.direction) == (EtfSort.EXPENSE_RATIO, SortDirection.ASC)
    assert (c.limit, c.offset) == (5, 10)


def test_defaults_sort_to_net_assets_desc():
    repo = _FakeSearchRepo()
    _client(repo).get("/stocks/etfs")
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)
    assert c.category is None


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


# --- GET /stocks/etf/{ticker} (the detail card) -------------------------------------------


class _FakeDetailUseCase:
    """Stands in for GetEtfDetail; returns a canned detail or raises. Records the ``include`` it
    was handed so the endpoint's query-param pass-through can be asserted."""

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
            "three_year_return": 20.41,  # Yahoo annualized
            "five_year_return": 13.01,
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
        "three_year_return": None,
        "five_year_return": None,
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
