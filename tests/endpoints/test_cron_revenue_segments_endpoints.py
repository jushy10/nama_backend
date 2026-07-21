from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_revenue_segments_endpoints as cron
from app.stocks.revenue_segments.use_cases import RevenueSegmentsSyncReport


class _FakeRunner:
    def __init__(self, report: RevenueSegmentsSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> RevenueSegmentsSyncReport:
        self.calls.append(limit)
        return self._report


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    # The auth guard is covered on its own in test_cron_auth.py; no-op it here.
    app.dependency_overrides[cron.require_cron_token] = lambda: None
    return TestClient(app)


def _drain() -> None:
    assert cron._sync_lock.acquire(timeout=2), "background sweep did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_sweep_with_the_limit():
    fake = _FakeRunner(RevenueSegmentsSyncReport(refreshed=7, failed=2, limit=50))
    resp = _client(fake).post("/internal/revenue-segments/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]


def test_defaults_to_unlimited_when_omitted():
    fake = _FakeRunner(RevenueSegmentsSyncReport(refreshed=0, failed=0, limit=None))
    resp = _client(fake).post("/internal/revenue-segments/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(RevenueSegmentsSyncReport(0, 0, 1))
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/revenue-segments/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(RevenueSegmentsSyncReport(0, 0, 1))
    assert _client(fake).post("/internal/revenue-segments/sync?limit=0").status_code == 422
    assert fake.calls == []
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_runner_is_wired_without_any_api_key():
    # SEC EDGAR needs no credential, so the DI returns the real unit of work with no key set.
    assert cron.get_sync_runner() is cron.run_revenue_segments_sync
