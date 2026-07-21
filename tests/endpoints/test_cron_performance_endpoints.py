from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_performance_endpoints as cron
from app.stocks.performance.use_cases import PerformanceSyncReport


class _FakeRunner:
    def __init__(self, report: PerformanceSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> PerformanceSyncReport:
        self.calls.append(limit)
        return self._report


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    # The auth guard is covered on its own in test_cron_auth.py; no-op it here so these
    # controller tests don't need a token.
    app.dependency_overrides[cron.require_cron_token] = lambda: None
    return TestClient(app)


def _drain() -> None:
    assert cron._sync_lock.acquire(timeout=2), "background sweep did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_sweep_with_the_limit():
    fake = _FakeRunner(PerformanceSyncReport(refreshed=7, skipped=2, limit=50))
    resp = _client(fake).post("/internal/performance/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]  # the query limit reached the runner


def test_defaults_to_unlimited_when_omitted():
    fake = _FakeRunner(PerformanceSyncReport(refreshed=0, skipped=0, limit=None))
    resp = _client(fake).post("/internal/performance/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]  # None => the sweep processes every screened stock


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(PerformanceSyncReport(0, 0, 1))
    # Simulate a sweep in flight by holding the guard, so the endpoint can't start another.
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/performance/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []  # nothing started while one was running
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(PerformanceSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before anything is scheduled.
    assert _client(fake).post("/internal/performance/sync?limit=0").status_code == 422
    assert fake.calls == []
    # The guard must be free — a rejected request must never strand it.
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_runner_no_ops_when_alpaca_keys_are_unset(monkeypatch):
    # A background runner isn't an HTTP context, so unset Alpaca keys are a logged no-op (an
    # empty report), not a 503 — the sweep just does nothing until the keys are configured.
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    report = cron.run_stock_performance_sync(None)
    assert report == PerformanceSyncReport(refreshed=0, skipped=0, limit=None)


def test_get_sync_runner_returns_the_real_unit_of_work():
    assert cron.get_sync_runner() is cron.run_stock_performance_sync
