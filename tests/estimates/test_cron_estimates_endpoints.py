"""Tests for the estimates cron endpoint (POST /internal/estimates/sync).

Offline: a fake SyncAnalystEstimates is injected through dependency_overrides, so this
checks only the controller — that it invokes the use case with the requested limit,
presents the summary, validates the limit, and gates on a missing FMP key — without
touching FMP or the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.stocks.estimates import cron_estimates_endpoints as cron
from app.stocks.estimates.use_cases import EstimatesSyncReport, SyncAnalystEstimates


class FakeSync:
    """Stands in for SyncAnalystEstimates; records the limit it was called with."""

    def __init__(self, report: EstimatesSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def execute(self, *, limit: int | None = None) -> EstimatesSyncReport:
        self.calls.append(limit)
        return self._report


def _client_with(fake: FakeSync) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_estimates] = lambda: fake
    return TestClient(app)


def test_runs_the_sync_and_returns_the_summary():
    fake = FakeSync(EstimatesSyncReport(refreshed=7, failed=2, limit=50))
    resp = _client_with(fake).post("/internal/estimates/sync?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"refreshed": 7, "failed": 2, "limit": 50}
    assert fake.calls == [50]  # the query limit reached the use case


def test_defaults_the_limit_when_omitted():
    default = SyncAnalystEstimates.DEFAULT_LIMIT
    fake = FakeSync(EstimatesSyncReport(refreshed=0, failed=0, limit=default))
    resp = _client_with(fake).post("/internal/estimates/sync")
    assert resp.status_code == 200
    assert fake.calls == [default]


def test_rejects_an_out_of_range_limit():
    fake = FakeSync(EstimatesSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before the use case is invoked.
    assert _client_with(fake).post("/internal/estimates/sync?limit=0").status_code == 422
    assert fake.calls == []


def test_missing_fmp_key_is_a_503(monkeypatch):
    # Exercise the real DI (no use-case override) with no key in the environment.
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(cron.router)
    # get_sync_estimates depends on get_db; stub it so the 503 path needs no real DB.
    app.dependency_overrides[get_db] = lambda: None
    resp = TestClient(app).post("/internal/estimates/sync")
    assert resp.status_code == 503
