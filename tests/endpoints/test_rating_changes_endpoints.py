"""Tests for the rating-changes read endpoint (GET /stocks/{symbol}/rating-changes).

Offline: a fake GetStockRatingChanges is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape (the event
fields + the derived is_upgrade/is_downgrade), the cache header, empty coverage as a 200
(not a 404), and the error mapping — without touching Yahoo or the database.
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import rating_changes_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.entities import AnalystRatingChanges, RatingChange


class _FakeUseCase:
    """Stands in for GetStockRatingChanges; returns a canned run or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> AnalystRatingChanges:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_rating_changes_use_case] = lambda: fake
    return TestClient(app)


def test_presents_the_events_with_derived_direction_flags():
    changes = AnalystRatingChanges(
        "AAPL",
        (
            RatingChange(
                "TD Cowen",
                date(2026, 6, 9),
                action="up",
                from_grade="Hold",
                to_grade="Buy",
                target_current=350.0,
                target_prior=335.0,
            ),
            RatingChange("KGI Securities", date(2026, 5, 1), action="down", to_grade="Hold"),
        ),
    )
    resp = _client(_FakeUseCase(result=changes)).get("/stocks/AAPL/rating-changes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    first = body["changes"][0]
    assert first["firm"] == "TD Cowen"
    assert first["from_grade"] == "Hold" and first["to_grade"] == "Buy"
    assert first["target_current"] == 350.0 and first["target_prior"] == 335.0
    assert first["is_upgrade"] is True and first["is_downgrade"] is False
    assert body["changes"][1]["is_downgrade"] is True


def test_sets_the_cache_header():
    fake = _FakeUseCase(
        result=AnalystRatingChanges("AAPL", (RatingChange("A Firm", date(2026, 6, 1)),))
    )
    resp = _client(fake).get("/stocks/AAPL/rating-changes")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_empty_coverage_is_a_200_with_no_events():
    fake = _FakeUseCase(result=AnalystRatingChanges("ZZZZ", ()))
    resp = _client(fake).get("/stocks/ZZZZ/rating-changes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["changes"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/123/rating-changes").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/rating-changes").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/AAPL/rating-changes").status_code == 502
