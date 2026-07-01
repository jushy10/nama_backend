"""Tests for the quarterly-earnings HTTP endpoints.

Offline: fakes injected through ``dependency_overrides`` + FastAPI's ``TestClient``, so this
checks only the controllers/presenters — never Yahoo or the database. Both endpoints of the
slice live here: the read endpoint (``GET /stocks/{symbol}/earnings/quarterly``) and the
cron endpoint (``POST /internal/earnings/quarterly/sync``).
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.earnings.quarterly import router as quarterly_router
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.use_cases import (
    QuarterlyEarningsSyncReport,
    SyncQuarterlyEarnings,
)
from app.stocks.endpoints import cron_quarterly_earnings_endpoints as cron


# ─────────────── read endpoint: GET /stocks/{symbol}/earnings/quarterly ───────────────


class _FakeReadUseCase:
    """Stands in for GetQuarterlyEarnings; returns a canned timeline or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> QuarterlyEarningsTimeline:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _read_client(fake: _FakeReadUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(quarterly_router.router)
    app.dependency_overrides[quarterly_router.get_quarterly_earnings_use_case] = (
        lambda: fake
    )
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


def test_read_presents_the_timeline_with_counts():
    fake = _FakeReadUseCase(result=_timeline())
    resp = _read_client(fake).get("/stocks/AAPL/earnings/quarterly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert (body["count"], body["reported_count"], body["upcoming_count"]) == (2, 1, 1)

    reported, upcoming = body["quarters"]
    assert (reported["fiscal_year"], reported["fiscal_quarter"]) == (2025, 4)
    assert reported["eps_actual"] == 3.3 and reported["eps_surprise_percent"] == 10.0
    assert reported["beat"] is True and reported["is_reported"] is True
    assert upcoming["eps_actual"] is None and upcoming["revenue_estimate"] == 100e9
    assert upcoming["beat"] is None and upcoming["is_reported"] is False
    assert fake.calls == ["AAPL"]


def test_read_empty_timeline_is_a_200_with_no_quarters():
    fake = _FakeReadUseCase(result=QuarterlyEarningsTimeline("ZZZZ", ()))
    resp = _read_client(fake).get("/stocks/ZZZZ/earnings/quarterly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0 and body["quarters"] == []


def test_read_bad_symbol_is_a_400():
    fake = _FakeReadUseCase(error=ValueError("'123' is not a valid stock symbol."))
    resp = _read_client(fake).get("/stocks/123/earnings/quarterly")
    assert resp.status_code == 400


# ─────────────── cron endpoint: POST /internal/earnings/quarterly/sync ───────────────


class _FakeSync:
    """Stands in for SyncQuarterlyEarnings; records the limit it was called with."""

    def __init__(self, report: QuarterlyEarningsSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def execute(self, *, limit: int | None = None) -> QuarterlyEarningsSyncReport:
        self.calls.append(limit)
        return self._report


def _cron_client(fake: _FakeSync) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_quarterly_earnings] = lambda: fake
    return TestClient(app)


def test_cron_runs_the_sync_and_returns_the_summary():
    fake = _FakeSync(QuarterlyEarningsSyncReport(refreshed=7, failed=2, limit=50))
    resp = _cron_client(fake).post("/internal/earnings/quarterly/sync?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"refreshed": 7, "failed": 2, "limit": 50}
    assert fake.calls == [50]  # the query limit reached the use case


def test_cron_defaults_the_limit_when_omitted():
    default = SyncQuarterlyEarnings.DEFAULT_LIMIT
    fake = _FakeSync(QuarterlyEarningsSyncReport(refreshed=0, failed=0, limit=default))
    resp = _cron_client(fake).post("/internal/earnings/quarterly/sync")
    assert resp.status_code == 200
    assert fake.calls == [default]


def test_cron_rejects_an_out_of_range_limit():
    fake = _FakeSync(QuarterlyEarningsSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before the use case is invoked.
    assert (
        _cron_client(fake)
        .post("/internal/earnings/quarterly/sync?limit=0")
        .status_code
        == 422
    )
    assert fake.calls == []


def test_cron_sync_is_wired_without_any_api_key():
    # yfinance needs no credential, so the real DI builds the use case with no key set.
    use_case = cron.get_sync_quarterly_earnings(db=None)
    assert isinstance(use_case, SyncQuarterlyEarnings)
