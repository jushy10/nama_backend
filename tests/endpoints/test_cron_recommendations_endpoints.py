"""Tests for the recommendations cron endpoint (POST /internal/recommendations/sync).

Offline: a fake SyncRecommendations is injected through dependency_overrides, so this
checks only the controller — that it invokes the use case with the requested limit,
presents the summary, and validates the limit — without touching Yahoo or the database.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_recommendations_endpoints as cron
from app.stocks.recommendations.use_cases import (
    RecommendationsSyncReport,
    SyncRecommendations,
)


class _FakeSync:
    """Stands in for SyncRecommendations; records the limit it was called with."""

    def __init__(self, report: RecommendationsSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def execute(self, *, limit: int | None = None) -> RecommendationsSyncReport:
        self.calls.append(limit)
        return self._report


def _client(fake: _FakeSync) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_recommendations] = lambda: fake
    return TestClient(app)


def test_runs_the_sync_and_returns_the_summary():
    fake = _FakeSync(RecommendationsSyncReport(refreshed=7, failed=2, limit=50))
    resp = _client(fake).post("/internal/recommendations/sync?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"refreshed": 7, "failed": 2, "limit": 50}
    assert fake.calls == [50]  # the query limit reached the use case


def test_defaults_the_limit_when_omitted():
    default = SyncRecommendations.DEFAULT_LIMIT
    fake = _FakeSync(RecommendationsSyncReport(refreshed=0, failed=0, limit=default))
    resp = _client(fake).post("/internal/recommendations/sync")
    assert resp.status_code == 200
    assert fake.calls == [default]


def test_rejects_an_out_of_range_limit():
    fake = _FakeSync(RecommendationsSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before the use case is invoked.
    assert (
        _client(fake).post("/internal/recommendations/sync?limit=0").status_code == 422
    )
    assert fake.calls == []


def test_sync_is_wired_without_any_api_key():
    # yfinance needs no credential, so the real DI builds the use case with no key set.
    use_case = cron.get_sync_recommendations(db=None)
    assert isinstance(use_case, SyncRecommendations)
