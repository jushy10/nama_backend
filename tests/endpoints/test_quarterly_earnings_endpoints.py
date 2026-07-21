from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.endpoints import quarterly_earnings_endpoints as endpoints


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_quarterly_earnings_use_case] = lambda: fake
    return TestClient(app)


def _timeline() -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(
            QuarterlyEarnings(
                fiscal_year=2025,
                fiscal_quarter=4,
                period_end=date(2025, 12, 31),
                report_date=date(2026, 2, 1),
                eps_actual=3.3,
                eps_estimate=3.0,
                eps_surprise=0.3,
                eps_surprise_percent=10.0,
                revenue_estimate=None,
                revenue_actual=5.0e9,
            ),
            QuarterlyEarnings(
                fiscal_year=2026,
                fiscal_quarter=1,
                period_end=date(2026, 3, 31),
                report_date=date(2026, 5, 1),
                eps_actual=None,
                eps_estimate=3.1,
                eps_surprise=None,
                eps_surprise_percent=None,
                revenue_estimate=100e9,
            ),
        ),
    )


def test_presents_the_timeline_with_counts():
    fake = _FakeUseCase(result=_timeline())
    resp = _client(fake).get("/stocks/AAPL/earnings/quarterly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert (body["count"], body["reported_count"], body["upcoming_count"]) == (2, 1, 1)

    reported, upcoming = body["quarters"]
    assert (reported["fiscal_year"], reported["fiscal_quarter"]) == (2025, 4)
    assert reported["eps_actual"] == 3.3 and reported["eps_surprise_percent"] == 10.0
    assert reported["revenue_actual"] == 5.0e9 and reported["revenue_estimate"] is None
    assert reported["beat"] is True and reported["is_reported"] is True
    assert upcoming["eps_actual"] is None and upcoming["revenue_estimate"] == 100e9
    assert upcoming["revenue_actual"] is None
    assert upcoming["beat"] is None and upcoming["is_reported"] is False
    assert fake.calls == ["AAPL"]


def test_empty_timeline_is_a_200_with_no_quarters():
    fake = _FakeUseCase(result=QuarterlyEarningsTimeline("ZZZZ", ()))
    resp = _client(fake).get("/stocks/ZZZZ/earnings/quarterly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0 and body["quarters"] == []


def test_bad_symbol_is_a_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    resp = _client(fake).get("/stocks/123/earnings/quarterly")
    assert resp.status_code == 400
