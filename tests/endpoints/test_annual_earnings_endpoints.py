from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domains.financials.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.endpoints import annual_earnings_endpoints as endpoints
from app.endpoints.error_handlers import register_error_handlers


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def run(self, symbol: str) -> AnnualEarningsTimeline:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    register_error_handlers(app)  # the endpoint has no try/except; the handlers translate
    # Overriding the shim replaces the whole construction chain (db session included).
    app.dependency_overrides[endpoints.get_get_annual_earnings] = lambda: fake
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
                eps_actual_consensus=6.4,
                fcf_per_share=2.5,
                ocf_per_share=3.6,
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
    assert reported["eps_actual_consensus"] == 6.4
    # Per-year cash flow surfaces on the reported year; the upcoming year carries neither.
    assert reported["fcf_per_share"] == 2.5 and reported["ocf_per_share"] == 3.6
    assert reported["is_reported"] is True
    assert upcoming["fiscal_year"] == 2025
    assert upcoming["eps_actual"] is None and upcoming["revenue_estimate"] == 420e9
    assert upcoming["revenue_actual"] is None and upcoming["net_income"] is None
    assert upcoming["eps_actual_consensus"] is None
    assert upcoming["fcf_per_share"] is None and upcoming["ocf_per_share"] is None
    assert upcoming["is_reported"] is False
    # Only one reported year here, so the trailing YoY snapshot has no prior to compare.
    assert body["revenue_growth_yoy"] is None and body["eps_growth_yoy"] is None
    assert fake.calls == ["AAPL"]


def test_presents_latest_trailing_yoy_when_two_reported_years():
    timeline = AnnualEarningsTimeline(
        symbol="AAPL",
        years=(
            AnnualEarnings(
                fiscal_year=2023,
                period_end=date(2023, 12, 31),
                eps_actual=4.5,
                eps_estimate=None,
                revenue_actual=300e9,
                revenue_estimate=None,
                net_income=80e9,
                eps_actual_consensus=5.0,
                fcf_per_share=2.0,
            ),
            AnnualEarnings(
                fiscal_year=2024,
                period_end=date(2024, 12, 31),
                eps_actual=6.0,
                eps_estimate=None,
                revenue_actual=360e9,
                revenue_estimate=None,
                net_income=100e9,
                eps_actual_consensus=6.0,
                fcf_per_share=2.5,
            ),
        ),
    )
    resp = _client(_FakeUseCase(result=timeline)).get("/stocks/AAPL/earnings/annual")
    assert resp.status_code == 200
    body = resp.json()
    # Trailing YoY: revenue (360-300)/300 = +20%; eps on the consensus basis (6.0-5.0)/5.0 = +20%;
    # fcf/share (2.5-2.0)/2.0 = +25%.
    assert body["revenue_growth_yoy"] == 20.0
    assert body["eps_growth_yoy"] == 20.0
    assert body["fcf_growth_yoy"] == 25.0


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


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/earnings/annual").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "boom"))
    assert _client(fake).get("/stocks/AAPL/earnings/annual").status_code == 502
