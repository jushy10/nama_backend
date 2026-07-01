"""Tests for the annual-earnings read endpoint (GET /stocks/{symbol}/earnings/annual).

Offline: a fake GetAnnualEarnings is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape and counts, an
empty timeline as a 200 (not a 404), and bad input as a 400 — without touching Yahoo or the
database.
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.endpoints import annual_earnings_endpoints as endpoints


class _FakeUseCase:
    """Stands in for GetAnnualEarnings; returns a canned timeline or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> AnnualEarningsTimeline:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_annual_earnings_use_case] = lambda: fake
    return TestClient(app)


def _timeline() -> AnnualEarningsTimeline:
    return AnnualEarningsTimeline(
        symbol="AAPL",
        years=(
            AnnualEarnings(
                fiscal_year=2024,
                period_end=date(2024, 12, 31),
                eps_actual=6.0,
                eps_estimate=None,
                revenue_actual=400e9,
                revenue_estimate=None,
                net_income=100e9,
            ),
            AnnualEarnings(
                fiscal_year=2025,
                period_end=date(2025, 12, 31),
                eps_actual=None,
                eps_estimate=6.5,
                revenue_actual=None,
                revenue_estimate=420e9,
            ),
        ),
    )


def test_presents_the_timeline_with_counts():
    fake = _FakeUseCase(result=_timeline())
    resp = _client(fake).get("/stocks/AAPL/earnings/annual")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert (body["count"], body["reported_count"], body["upcoming_count"]) == (2, 1, 1)

    reported, upcoming = body["years"]
    assert reported["fiscal_year"] == 2024
    assert reported["eps_actual"] == 6.0 and reported["revenue_actual"] == 400e9
    assert reported["net_income"] == 100e9 and reported["revenue_estimate"] is None
    assert reported["is_reported"] is True
    assert upcoming["fiscal_year"] == 2025
    assert upcoming["eps_actual"] is None and upcoming["revenue_estimate"] == 420e9
    assert upcoming["revenue_actual"] is None and upcoming["net_income"] is None
    assert upcoming["is_reported"] is False
    assert fake.calls == ["AAPL"]


def test_empty_timeline_is_a_200_with_no_years():
    fake = _FakeUseCase(result=AnnualEarningsTimeline("ZZZZ", ()))
    resp = _client(fake).get("/stocks/ZZZZ/earnings/annual")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0 and body["years"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    resp = _client(fake).get("/stocks/123/earnings/annual")
    assert resp.status_code == 400
