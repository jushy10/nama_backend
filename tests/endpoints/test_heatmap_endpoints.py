"""Endpoint tests for GET /market/heatmap.

Offline: a fake use case injected through dependency_overrides + FastAPI's TestClient, so the
route's controller/presenter/validation are exercised with no DB or vendor. The use case itself
is unit-tested in tests/heatmap/test_heatmap.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.stocks.entities import StockPerformance
from app.stocks.endpoints import heatmap_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope


class _FakeUseCase:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.scope = None

    def execute(self, scope):
        self.scope = scope
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app.dependency_overrides[endpoints.get_heatmap_use_case] = lambda: fake
    return TestClient(app)


def _teardown():
    app.dependency_overrides.pop(endpoints.get_heatmap_use_case, None)


def _a_map(scope=HeatMapScope.SP500) -> HeatMap:
    rows = (
        HeatMapRow("NVDA", "NVIDIA", "technology", "semiconductors", 3e12),
        HeatMapRow("JPM", "JPMorgan", "financials", "banks", 6e11),
    )
    perf = {
        "NVDA": StockPerformance(
            one_week=2.0,
            one_month=8.0,
            three_month=None,
            six_month=None,
            ytd=40.0,
            one_year=120.0,
        )
    }
    return HeatMap.build(scope, rows, {"NVDA": -0.99, "JPM": 1.70}, perf)


def test_returns_the_expected_json_shape():
    try:
        resp = _client(_FakeUseCase(result=_a_map())).get("/market/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "sp500"
        assert body["count"] == 2
        # Sectors largest-cap first: technology (3e12) before financials (6e11).
        assert [s["sector"] for s in body["sectors"]] == ["technology", "financials"]
        tech = body["sectors"][0]
        assert tech["industries"][0]["industry"] == "semiconductors"
        cell = tech["industries"][0]["stocks"][0]
        # Trailing windows serialize under the finance-style aliases ("1w"/"1m"/…), null
        # for a window with no history; a name absent from the perf map → performance null.
        assert cell == {
            "ticker": "NVDA",
            "name": "NVIDIA",
            "market_cap": 3e12,
            "change_percent": -0.99,
            "performance": {
                "1w": 2.0,
                "1m": 8.0,
                "3m": None,
                "6m": None,
                "ytd": 40.0,
                "1y": 120.0,
            },
        }
        assert tech["industries"][0]["stocks"][0]["performance"]["1y"] == 120.0
        jpm = body["sectors"][1]["industries"][0]["stocks"][0]
        assert jpm["performance"] is None
        assert resp.headers["cache-control"] == "public, max-age=60"
    finally:
        _teardown()


def test_defaults_to_sp500_scope():
    fake = _FakeUseCase(result=_a_map())
    try:
        _client(fake).get("/market/heatmap")
        assert fake.scope is HeatMapScope.SP500
    finally:
        _teardown()


def test_index_param_selects_nasdaq100():
    fake = _FakeUseCase(result=_a_map(HeatMapScope.NASDAQ100))
    try:
        resp = _client(fake).get("/market/heatmap", params={"index": "nasdaq100"})
        assert resp.status_code == 200
        assert fake.scope is HeatMapScope.NASDAQ100
    finally:
        _teardown()


def test_unknown_index_is_400():
    try:
        resp = _client(_FakeUseCase(result=_a_map())).get(
            "/market/heatmap", params={"index": "dow"}
        )
        assert resp.status_code == 400
    finally:
        _teardown()


def test_data_unavailable_is_502():
    fake = _FakeUseCase(error=StockDataUnavailable("quotes", "boom"))
    try:
        resp = _client(fake).get("/market/heatmap")
        assert resp.status_code == 502
    finally:
        _teardown()
