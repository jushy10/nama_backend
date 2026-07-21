from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints.cron import fundamentals_endpoints as cron
from app.stocks.catalog.fundamentals.use_cases import FundamentalsSyncReport


class _FakeRunner:
    def __init__(self, report: FundamentalsSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> FundamentalsSyncReport:
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
    fake = _FakeRunner(FundamentalsSyncReport(refreshed=7, failed=2, limit=50))
    resp = _client(fake).post("/internal/fundamentals/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]  # the query limit reached the runner


def test_defaults_to_unlimited_when_omitted():
    fake = _FakeRunner(FundamentalsSyncReport(refreshed=0, failed=0, limit=None))
    resp = _client(fake).post("/internal/fundamentals/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]  # None => the sweep processes every stock


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(FundamentalsSyncReport(0, 0, 1))
    # Simulate a sweep in flight by holding the guard, so the endpoint can't start another.
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/fundamentals/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []  # nothing started while one was running
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(FundamentalsSyncReport(0, 0, 1))
    # limit must be >= 1; 0 fails validation before anything is scheduled.
    assert _client(fake).post("/internal/fundamentals/sync?limit=0").status_code == 422
    assert fake.calls == []
    # The guard must be free — a rejected request must never strand it.
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_runner_is_wired_without_any_api_key():
    # yfinance needs no credential, so the DI returns the real unit of work with no key set.
    assert cron.get_sync_runner() is cron.run_fundamentals_sync
