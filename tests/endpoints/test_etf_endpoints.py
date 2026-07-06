"""Tests for the ETF search endpoint (GET /stocks/etfs).

Offline: the search use case is built over an in-memory fake repository and injected through
dependency_overrides, so this checks the controller + presenter + query binding — the response
envelope, the q/sort/order/paging params reaching the use case, and the enum validation — with
no database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import etf_endpoints as endpoints
from app.stocks.etfs.entities import (
    EtfSearchPage,
    EtfSearchResult,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.repository import EtfSearchRepository
from app.stocks.etfs.use_cases import SearchEtfs


class _FakeSearchRepo(EtfSearchRepository):
    """Records the criteria it was handed and returns a page built from canned rows."""

    def __init__(self, results=()) -> None:
        self._results = tuple(results)
        self.criteria = None

    def search(self, criteria):
        self.criteria = criteria
        return EtfSearchPage(
            results=self._results,
            total=len(self._results),
            limit=criteria.limit,
            offset=criteria.offset,
        )


def _result(ticker, **kw) -> EtfSearchResult:
    base = dict(name=None, exchange=None, net_assets=1e11, expense_ratio=0.1, ytd_return=5.0)
    base.update(kw)
    return EtfSearchResult(ticker=ticker, **base)


def _client(repo: _FakeSearchRepo) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_search_use_case] = lambda: SearchEtfs(repo)
    return TestClient(app)


def test_returns_a_page_envelope_with_rows():
    repo = _FakeSearchRepo(
        [
            _result(
                "SPY",
                name="SPDR S&P 500 ETF Trust",
                exchange="NYSEARCA",
                net_assets=5e11,
                expense_ratio=0.09,
                ytd_return=6.5,
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
        "exchange": "NYSEARCA",
        "net_assets": 5e11,
        "expense_ratio": 0.09,
        "ytd_return": 6.5,
    }


def test_passes_query_sort_and_paging_to_the_use_case():
    repo = _FakeSearchRepo()
    resp = _client(repo).get("/stocks/etfs?q=gold&sort=ytd_return&order=asc&limit=5&offset=10")
    assert resp.status_code == 200
    c = repo.criteria
    assert c.query == "gold"
    assert (c.sort, c.direction) == (EtfSort.YTD_RETURN, SortDirection.ASC)
    assert (c.limit, c.offset) == (5, 10)


def test_defaults_sort_to_net_assets_desc():
    repo = _FakeSearchRepo()
    _client(repo).get("/stocks/etfs")
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)


def test_rejects_an_unknown_sort():
    repo = _FakeSearchRepo()
    # An out-of-enum sort is a 422 from the query binding, before the use case runs.
    assert _client(repo).get("/stocks/etfs?sort=bogus").status_code == 422


def test_sets_a_short_cache_header():
    repo = _FakeSearchRepo()
    resp = _client(repo).get("/stocks/etfs")
    assert resp.headers["cache-control"] == "public, max-age=60"
