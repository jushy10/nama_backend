from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import revenue_segments_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> RevenueSegmentation:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_revenue_segments_use_case] = lambda: fake
    return TestClient(app)


def test_presents_the_segmentation():
    seg = RevenueSegmentation(
        "GOOGL",
        (
            RevenueSegment(2024, date(2024, 12, 31), SegmentAxis.BUSINESS, "GoogleCloudMember", 58.7e9),
            RevenueSegment(2024, date(2024, 12, 31), SegmentAxis.GEOGRAPHY, "US", 194.2e9),
            RevenueSegment(2023, date(2023, 12, 31), SegmentAxis.BUSINESS, "GoogleCloudMember", 33.1e9),
        ),
    )
    resp = _client(_FakeUseCase(result=seg)).get("/stocks/GOOGL/revenue-segments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "GOOGL"
    assert body["count"] == 3
    assert body["fiscal_years"] == [2024, 2023]
    assert body["latest_fiscal_year"] == 2024
    first = body["segments"][0]
    assert first["axis"] == "business_segment"  # the enum slug
    assert first["member"] == "GoogleCloudMember"
    assert first["label"] == "Google Cloud"  # derived from the raw member
    assert first["value"] == 58.7e9


def test_sets_the_cache_header():
    seg = RevenueSegmentation(
        "GOOGL", (RevenueSegment(2024, date(2024, 12, 31), SegmentAxis.BUSINESS, "A", 1e9),)
    )
    resp = _client(_FakeUseCase(result=seg)).get("/stocks/GOOGL/revenue-segments")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_no_segments():
    resp = _client(_FakeUseCase(result=RevenueSegmentation("ZZZZ", ()))).get(
        "/stocks/ZZZZ/revenue-segments"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["fiscal_years"] == []
    assert body["latest_fiscal_year"] is None
    assert body["segments"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/123/revenue-segments").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/revenue-segments").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("GOOGL", "boom"))
    assert _client(fake).get("/stocks/GOOGL/revenue-segments").status_code == 502
