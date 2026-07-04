"""Tests for the universe cron endpoint (POST /internal/universe/sync).

Offline: a fake sync runner is injected through dependency_overrides, so this checks only the
controller — that it accepts a trigger, runs the sweep in the background with the requested
limit, guards against overlapping runs, and validates the limit — without touching Yahoo or
the database.

The sweep runs on a daemon thread, so the tests that expect it to run drain it first: the
endpoint holds ``_sync_lock`` from acceptance until the background thread finishes, so waiting
to re-acquire the lock is a deterministic "sweep done" barrier.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_universe_endpoints as cron
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport


class _FakeRunner:
    """Stands in for the real sync runner; records the limit it was called with and runs
    instantly, so the background sweep finishes at once."""

    def __init__(self, report: UniverseSyncReport) -> None:
        self._report = report
        self.calls: list[int | None] = []

    def __call__(self, limit: int | None = None) -> UniverseSyncReport:
        self.calls.append(limit)
        return self._report


def _report() -> UniverseSyncReport:
    return UniverseSyncReport(
        screened=1200, added=30, updated=1170, skipped=False, enriched=40, enrich_failed=2
    )


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    return TestClient(app)


def _drain() -> None:
    """Block until the background sweep has finished. The endpoint holds ``_sync_lock``
    until the daemon thread releases it, so re-acquiring the lock means the sweep is done."""
    assert cron._sync_lock.acquire(timeout=2), "background sweep did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_sweep_with_the_limit():
    fake = _FakeRunner(_report())
    resp = _client(fake).post("/internal/universe/sync?limit=50")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 50}
    _drain()
    assert fake.calls == [50]  # the query limit reached the runner


def test_defaults_the_limit_when_omitted():
    default = SyncUniverse.DEFAULT_LIMIT
    fake = _FakeRunner(_report())
    resp = _client(fake).post("/internal/universe/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": default}
    _drain()
    assert fake.calls == [default]


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    fake = _FakeRunner(_report())
    # Simulate a sweep in flight by holding the guard, so the endpoint can't start another.
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/universe/sync?limit=50")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 50}
        assert fake.calls == []  # nothing started while one was running
    finally:
        cron._sync_lock.release()


def test_rejects_an_out_of_range_limit():
    fake = _FakeRunner(_report())
    # limit must be >= 1; 0 fails validation before anything is scheduled.
    assert _client(fake).post("/internal/universe/sync?limit=0").status_code == 422
    assert fake.calls == []
    # The guard must be free — a rejected request must never strand it.
    assert cron._sync_lock.acquire(blocking=False)
    cron._sync_lock.release()


def test_runner_is_wired_without_any_api_key():
    # yfinance needs no credential, so the DI returns the real unit of work with no key set.
    assert cron.get_sync_runner() is cron.run_universe_sync
