from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domains.research.brief.use_cases import MarketBriefSyncReport
from app.endpoints.cron import market_brief_endpoints as cron


class _FakeRunner:
    def __init__(self, report: MarketBriefSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> MarketBriefSyncReport:
        self.calls.append(limit)
        return self._report


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    app.dependency_overrides[cron.require_cron_token] = lambda: None
    return TestClient(app)


def _drain() -> None:
    assert cron._sync_lock.acquire(timeout=2), "background generation did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_generation():
    fake = _FakeRunner(MarketBriefSyncReport(generated=True, brief_date=date(2026, 7, 14)))
    resp = _client(fake).post("/internal/market-brief/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]


def test_a_trigger_while_a_run_is_in_flight_is_a_noop():
    fake = _FakeRunner(MarketBriefSyncReport(generated=False, brief_date=date(2026, 7, 14)))
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/market-brief/sync")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": None}
        assert fake.calls == []
    finally:
        cron._sync_lock.release()


def test_runner_is_wired():
    assert cron.get_sync_runner() is cron.run_market_brief_sync
