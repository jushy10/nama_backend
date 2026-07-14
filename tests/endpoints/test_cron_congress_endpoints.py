"""Tests for the Congressional-trades cron endpoint (POST /internal/congress/sync).

Offline: a fake sync runner injected through ``dependency_overrides``, so this checks only the
controller — that it accepts a trigger, runs the sweep in the background with the requested limit,
guards against overlapping runs, and validates the limit — without touching the source or the DB.

The sweep runs on a daemon thread, so the tests that expect it to run drain it first: the endpoint
holds ``_sync_lock`` from acceptance until the background thread finishes, so re-acquiring the lock
is a deterministic "sweep done" barrier.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.congress.use_cases import CongressSyncReport
from app.stocks.endpoints import cron_congress_endpoints as cron


class _FakeRunner:
    def __init__(self, report: CongressSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> CongressSyncReport:
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
    fake = _FakeRunner(CongressSyncReport(fetched=100, stored=7, failed=0, limit=50))
    resp = _client(fake).post("/internal/congress/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]


def test_defaults_to_unlimited_when_omitted():
    fake = _FakeRunner(CongressSyncReport(fetched=0, stored=0, failed=0, limit=None))
    resp = _client(fake).post("/internal/congress/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": None}
    _drain()
    assert fake.calls == [None]


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(CongressSyncReport(0, 0, 0, 1))
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/congress/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(CongressSyncReport(0, 0, 0, 1))
    assert _client(fake).post("/internal/congress/sync?limit=0").status_code == 422
    assert fake.calls == []
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_runner_is_wired_without_any_api_key():
    # The community stock-watcher feeds need no credential, so the DI returns the real unit of work
    # with no key set.
    assert cron.get_sync_runner() is cron.run_congress_sync
