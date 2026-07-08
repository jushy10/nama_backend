"""Tests for the ETF read endpoints (GET /stocks/etfs, GET /stocks/etfs/categories, and
GET /stocks/etf/{ticker}).

Offline: the use cases are built over in-memory fakes and injected through dependency_overrides, so
this checks the controllers + presenters + query binding — the search response envelope, the
q/category/sort/order/paging params reaching the use case, the enum validation, the categories
menu, and for the detail card: the JSON shape (quote + stored facts + stored profile), the
404-for-non-ETF, the 502-on-quote-failure, the empty-profile serving, and the cache header — with
no database, Alpaca, or Yahoo.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import etf_endpoints as endpoints
from app.stocks.entities import Quote
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
    """Stands in for GetEtfDetail; returns a canned detail or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, ticker: str) -> EtfDetail:
        self.calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._result


def _detail_client(fake: _FakeDetailUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_etf_detail_use_case] = lambda: fake
    return TestClient(app)


def _a_detail(*, profile: EtfProfile | None = None) -> EtfDetail:
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
    return EtfDetail.assemble("VOO", quote, facts, profile)


def test_detail_returns_the_full_json_shape():
    fake = _FakeDetailUseCase(result=_a_detail())
    resp = _detail_client(fake).get("/stocks/etf/VOO")

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
        "net_assets": 1_701_513_003_008.0,
        "expense_ratio": 0.03,
        "fund_family": "Vanguard",
        "nav": 685.28,
        "dividend_yield": 1.03,
        "ytd_return": 11.25,
        "three_year_return": 20.41,
        "five_year_return": 13.01,
        "description": "The fund employs an indexing investment approach.",
        "top_holdings": [{"ticker": "NVDA", "name": "NVIDIA Corp", "weight": 7.89}],
        "sector_weightings": [{"sector": "technology", "weight": 39.13}],
    }
    assert fake.calls == ["VOO"]


def test_detail_serves_null_and_empty_enrichment_when_the_profile_is_empty():
    # A fund the sync hasn't profile-enriched yet has an empty stored profile — the quote + stored
    # facts still serve on a 200, with the enrichment fields null and the lists empty.
    fake = _FakeDetailUseCase(result=_a_detail(profile=EtfProfile.empty()))
    resp = _detail_client(fake).get("/stocks/etf/VOO")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == 685.28  # quote still serves
    assert body["name"] == "Vanguard S&P 500 ETF"  # stored facts still serve
    assert body["expense_ratio"] == 0.03  # stored fact
    assert body["fund_family"] is None
    assert body["nav"] is None
    assert body["dividend_yield"] is None
    assert body["ytd_return"] is None
    assert body["description"] is None
    assert body["top_holdings"] == []
    assert body["sector_weightings"] == []


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
