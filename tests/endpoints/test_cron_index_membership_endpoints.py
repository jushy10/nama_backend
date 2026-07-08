"""Tests for the index-membership cron endpoint (POST /internal/index-membership/sync).

Offline: a fake sync runner is injected through dependency_overrides, so this checks only the
controller — it accepts a trigger, runs the reconcile in the background, and guards against
overlapping runs — without touching Wikipedia or the database. The Wikipedia source is keyless,
so (unlike the old Finnhub wiring) there's no key gate: the runner is always available. There's
no stalest-N limit here, so the endpoint passes 0 to the shared helper.

The reconcile runs on a daemon thread, so the test that expects it to run drains it first: the
endpoint holds ``_sync_lock`` from acceptance until the background thread finishes, so waiting to
re-acquire the lock is a deterministic "reconcile done" barrier.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import cron_index_membership_endpoints as cron
from app.stocks.index_membership.use_cases import IndexMembershipSyncReport


class _FakeRunner:
    """Stands in for the real sync runner; records the (ignored) limit it was called with and
    runs instantly, so the background reconcile finishes at once."""

    def __init__(self, report: IndexMembershipSyncReport) -> None:
        self._report = report
        self.calls: list[int] = []

    def __call__(self, limit: int) -> IndexMembershipSyncReport:
        self.calls.append(limit)
        return self._report


def _report() -> IndexMembershipSyncReport:
    return IndexMembershipSyncReport(
        sp500_members=503,
        sp500_marked=3,
        sp500_cleared=1,
        sp500_skipped=False,
        nasdaq100_members=101,
        nasdaq100_marked=1,
        nasdaq100_cleared=0,
        nasdaq100_skipped=False,
    )


def _client(fake: _FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(cron.router)
    app.dependency_overrides[cron.get_sync_runner] = lambda: fake
    # The auth guard is covered on its own in test_cron_auth.py; no-op it here so these
    # controller tests don't need a token.
    app.dependency_overrides[cron.require_cron_token] = lambda: None
    return TestClient(app)


def _drain() -> None:
    """Block until the background reconcile has finished — re-acquiring the lock the endpoint
    holds until the daemon thread releases it is a deterministic "done" barrier."""
    assert cron._sync_lock.acquire(timeout=2), "background reconcile did not finish in time"
    cron._sync_lock.release()


def test_accepts_the_trigger_and_runs_the_reconcile():
    fake = _FakeRunner(_report())
    resp = _client(fake).post("/internal/index-membership/sync")
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "limit": 0}
    _drain()
    assert fake.calls == [0]  # full reconcile; the shared limit is passed as 0 and ignored


def test_a_trigger_while_a_reconcile_runs_is_a_noop():
    fake = _FakeRunner(_report())
    # Simulate a reconcile in flight by holding the guard, so the endpoint can't start another.
    assert cron._sync_lock.acquire(blocking=False)
    try:
        resp = _client(fake).post("/internal/index-membership/sync")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_running", "limit": 0}
        assert fake.calls == []  # nothing started while one was running
    finally:
        cron._sync_lock.release()


def test_get_sync_runner_wires_the_real_runner():
    # Keyless source: no key to gate on, so the runner is always available.
    assert cron.get_sync_runner() is cron.run_index_membership_sync
