"""Tests for the annual-earnings cron endpoint (POST /internal/earnings/annual/sync).

Offline: a fake SyncAnnualEarnings is injected through dependency_overrides, so this checks
only the controller — that it invokes the use case with the requested limit, presents the
summary, and validates the limit — without touching Yahoo or the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.earnings.annual.use_cases import (
    AnnualEarningsSyncReport,
    SyncAnnualEarnings,
)
from app.stocks.endpoints import cron_annual_earnings_endpoints as cron


class _FakeSync:
    """Stands in for SyncAnnualEarnings; records the limit it was called with."""

    def __init__(self, report: AnnualEarningsSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def execute(self, *, limit: int | None = None) -> AnnualEarningsSyncReport:
        self.calls.append(limit)
        return self._report


def _client(fake: _FakeSync) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_annual_earnings] = lambda: fake
    return TestClient(app)


def test_runs_the_sync_and_returns_the_summary():
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=7, failed=2, limit=50))
    resp = _client(fake).post("/internal/earnings/annual/sync?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"refreshed": 7, "failed": 2, "limit": 50}
    assert fake.calls == [50]  # the query limit reached the use case


def test_defaults_the_limit_when_omitted():
    default = SyncAnnualEarnings.DEFAULT_LIMIT
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=0, failed=0, limit=default))
    resp = _client(fake).post("/internal/earnings/annual/sync")
    assert resp.status_code == 200
    assert fake.calls == [default]


def test_rejects_an_out_of_range_limit():
    fake = _FakeSync(AnnualEarningsSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before the use case is invoked.
    assert (
        _client(fake).post("/internal/earnings/annual/sync?limit=0").status_code == 422
    )
    assert fake.calls == []


def test_requires_the_cron_token_once_configured(monkeypatch):
    # The guard is opt-in: with CRON_SYNC_TOKEN set, a request without (or with the
    # wrong) bearer token is rejected before the use case runs; the right one passes.
    monkeypatch.setenv("CRON_SYNC_TOKEN", "s3cret")
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=1, failed=0, limit=10))
    client = _client(fake)

    assert client.post("/internal/earnings/annual/sync").status_code == 401
    assert (
        client.post(
            "/internal/earnings/annual/sync",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert fake.calls == []  # rejected before the sync ran

    ok = client.post(
        "/internal/earnings/annual/sync?limit=10",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert ok.status_code == 200
    assert fake.calls == [10]


def test_stays_open_while_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("CRON_SYNC_TOKEN", raising=False)
    fake = _FakeSync(AnnualEarningsSyncReport(refreshed=1, failed=0, limit=10))
    assert _client(fake).post("/internal/earnings/annual/sync").status_code == 200


def test_sync_is_wired_without_any_api_key():
    # yfinance needs no credential, so the real DI builds the use case with no key set.
    use_case = cron.get_sync_annual_earnings(db=None)
    assert isinstance(use_case, SyncAnnualEarnings)
