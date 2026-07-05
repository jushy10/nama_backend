"""Tests for the universe read endpoints (GET /stocks/ticker, GET /stocks/classifications).

Offline: fake use cases injected through dependency_overrides + FastAPI's TestClient, so this
checks only the controller + presenter — the JSON shape, the query-param → use-case
pass-through (raw, since normalization is the use case's job), FastAPI's enum/bounds validation
(422), the ValueError → 400 mapping, and the cache headers — without touching the database.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import universe_endpoints as endpoints
from app.stocks.universe.entities import (
    Classifications,
    SortDirection,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)


class _FakeSearch:
    """Stands in for SearchStocks; records the kwargs it was called with, returns a page."""

    def __init__(self, *, page=None, error=None) -> None:
        self._page = page
        self._error = error
        self.kwargs: dict | None = None

    def execute(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._page


class _FakeClassifications:
    """Stands in for ListClassifications; returns a canned Classifications."""

    def __init__(self, result: Classifications) -> None:
        self._result = result

    def execute(self) -> Classifications:
        return self._result


def _client(*, search=None, classifications=None) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    if search is not None:
        app.dependency_overrides[endpoints.get_search_use_case] = lambda: search
    if classifications is not None:
        app.dependency_overrides[endpoints.get_classifications_use_case] = (
            lambda: classifications
        )
    return TestClient(app)


def _a_page() -> StockSearchPage:
    return StockSearchPage(
        results=(
            StockSearchResult(
                ticker="NVDA",
                name="Nvidia",
                sector="technology",
                industry="semiconductors",
                market_cap=3e12,
                revenue_growth_yoy=61.6,
                eps_growth_yoy=587.4,
                in_sp500=True,
                in_nasdaq100=True,
            ),
        ),
        total=1,
        limit=25,
        offset=0,
    )


def test_search_returns_the_expected_json_shape():
    resp = _client(search=_FakeSearch(page=_a_page())).get("/stocks/ticker")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["total"], body["limit"], body["offset"], body["count"]) == (1, 25, 0, 1)
    (row,) = body["results"]
    assert row == {
        "ticker": "NVDA",
        "name": "Nvidia",
        "sector": "technology",
        "industry": "semiconductors",
        "market_cap": 3e12,
        "revenue_growth_yoy": 61.6,
        "eps_growth_yoy": 587.4,
        "in_sp500": True,
        "in_nasdaq100": True,
    }


def test_search_passes_query_params_through_to_the_use_case():
    fake = _FakeSearch(page=_a_page())
    resp = _client(search=fake).get(
        "/stocks/ticker",
        params={
            "q": "nv",
            "sector": "Technology",  # raw — the use case (not the endpoint) slugs it
            "industry": "semiconductors",
            "in_sp500": "true",
            "in_nasdaq100": "false",
            "sort": "revenue_growth",
            "order": "asc",
            "limit": "10",
            "offset": "20",
        },
    )

    assert resp.status_code == 200
    assert fake.kwargs == {
        "query": "nv",
        "sector": "Technology",
        "industry": "semiconductors",
        "in_sp500": True,
        "in_nasdaq100": False,
        "sort": StockSort.REVENUE_GROWTH,
        "direction": SortDirection.ASC,
        "limit": 10,
        "offset": 20,
    }


def test_search_uses_defaults_when_no_params_given():
    fake = _FakeSearch(page=_a_page())
    _client(search=fake).get("/stocks/ticker")

    assert fake.kwargs == {
        "query": None,
        "sector": None,
        "industry": None,
        "in_sp500": None,
        "in_nasdaq100": None,
        "sort": StockSort.MARKET_CAP,
        "direction": SortDirection.DESC,
        "limit": 25,
        "offset": 0,
    }


@pytest.mark.parametrize("param, value", [("sort", "bogus"), ("order", "sideways")])
def test_search_rejects_an_unknown_enum_value(param, value):
    resp = _client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={param: value}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 101, 9999])
def test_search_rejects_an_out_of_range_limit(limit):
    resp = _client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={"limit": limit}
    )
    assert resp.status_code == 422


def test_search_rejects_a_negative_offset():
    resp = _client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={"offset": -1}
    )
    assert resp.status_code == 422


def test_search_maps_a_value_error_to_400():
    fake = _FakeSearch(error=ValueError("bad filter"))
    resp = _client(search=fake).get("/stocks/ticker")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad filter"


def test_search_sets_a_short_cache_header():
    resp = _client(search=_FakeSearch(page=_a_page())).get("/stocks/ticker")
    assert resp.headers["Cache-Control"] == "public, max-age=60"


def test_classifications_returns_the_expected_json_shape():
    fake = _FakeClassifications(
        Classifications(("energy", "technology"), ("oil_gas", "semiconductors"))
    )
    resp = _client(classifications=fake).get("/stocks/classifications")

    assert resp.status_code == 200
    assert resp.json() == {
        "sectors": ["energy", "technology"],
        "industries": ["oil_gas", "semiconductors"],
    }


def test_classifications_sets_a_longer_cache_header():
    fake = _FakeClassifications(Classifications((), ()))
    resp = _client(classifications=fake).get("/stocks/classifications")

    assert resp.headers["Cache-Control"] == "public, max-age=300"
