"""Tests for the sync-status tracking (app/stocks/endpoints/sync_status.py) and its endpoint.

Offline and in-process: drives the tracker directly and the endpoint via TestClient. The
tracker registry is a module global (each cron module registers one at import), so a fixture
snapshots and restores it around every test — a test's throwaway tracker must not leak into the
endpoint's view for other tests.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import sync_status
from app.stocks.endpoints.background_sync import combined_reporter
from app.stocks.endpoints.sync_status_endpoints import router as status_router
from app.stocks.sync_progress import SyncOutcome, SyncProgress


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(sync_status._registry)
    try:
        yield
    finally:
        sync_status._registry.clear()
        sync_status._registry.update(saved)


# ───────────────────────────── SyncStatusTracker ─────────────────────────────


def test_a_fresh_tracker_is_idle_and_never_run():
    snap = sync_status.SyncStatusTracker("slice").snapshot()
    assert snap.state == "idle"
    assert snap.started_at is None
    assert (snap.done, snap.total, snap.ok, snap.failed, snap.skipped) == (0, None, 0, 0, 0)


def test_start_then_ticks_reflect_running_progress():
    tracker = sync_status.SyncStatusTracker("slice")
    tracker.start(limit=50)
    tracker(SyncProgress(1, 3, "AAA", SyncOutcome.OK))
    tracker(SyncProgress(2, 3, "BBB", SyncOutcome.FAILED, "unavailable"))
    tracker(SyncProgress(3, 3, "CCC", SyncOutcome.SKIPPED, "unclassified"))

    snap = tracker.snapshot()
    assert snap.state == "running"  # not finished yet
    assert snap.limit == 50
    assert (snap.done, snap.total) == (3, 3)
    assert (snap.ok, snap.failed, snap.skipped) == (1, 1, 1)
    assert snap.last_symbol == "CCC"
    assert snap.started_at is not None and snap.finished_at is None


def test_finish_flips_to_idle_and_keeps_the_final_counts():
    tracker = sync_status.SyncStatusTracker("slice")
    tracker.start(limit=None)
    tracker(SyncProgress(1, 1, "AAA", SyncOutcome.OK))
    tracker.finish()

    snap = tracker.snapshot()
    assert snap.state == "idle"
    assert snap.finished_at is not None
    assert (snap.done, snap.ok) == (1, 1)  # last run's counts remain readable
    assert snap.last_error is None


def test_start_resets_the_previous_runs_counts():
    tracker = sync_status.SyncStatusTracker("slice")
    tracker.start(limit=None)
    tracker(SyncProgress(1, 1, "AAA", SyncOutcome.FAILED))
    tracker.finish()

    tracker.start(limit=10)  # a new run
    snap = tracker.snapshot()
    assert snap.state == "running"
    assert (snap.done, snap.failed, snap.last_symbol) == (0, 0, None)
    assert snap.finished_at is None  # cleared for the new run


# ───────────────────────────────── track_run ─────────────────────────────────


def test_track_run_brackets_a_successful_sweep():
    tracker = sync_status.SyncStatusTracker("slice")
    with sync_status.track_run(tracker, limit=5):
        assert tracker.snapshot().state == "running"
    snap = tracker.snapshot()
    assert snap.state == "idle" and snap.last_error is None


def test_track_run_records_an_error_and_re_raises():
    tracker = sync_status.SyncStatusTracker("slice")
    with pytest.raises(RuntimeError):
        with sync_status.track_run(tracker, limit=5):
            raise RuntimeError("sweep boom")
    snap = tracker.snapshot()
    assert snap.state == "idle"
    assert snap.last_error is not None and "sweep boom" in snap.last_error


# ─────────────────────────────── combined_reporter ───────────────────────────


def test_combined_reporter_fans_out_and_skips_none():
    seen_a, seen_b = [], []
    reporter = combined_reporter(seen_a.append, None, seen_b.append)
    tick = SyncProgress(1, 1, "AAA", SyncOutcome.OK)

    reporter(tick)

    assert seen_a == [tick] and seen_b == [tick]  # both sinks got it; the None was ignored


def test_logging_and_status_both_receive_ticks(caplog):
    # The real wiring: the runner fans each tick to the log heartbeat AND the status tracker.
    from app.stocks.endpoints.background_sync import logging_progress_reporter

    tracker = sync_status.SyncStatusTracker("slice")
    reporter = combined_reporter(
        logging_progress_reporter("test sync", every=1), tracker
    )
    with caplog.at_level(logging.INFO, logger="app.stocks.endpoints.background_sync"):
        reporter(SyncProgress(1, 1, "AAA", SyncOutcome.OK))

    assert tracker.snapshot().done == 1  # tracker updated
    assert any("1/1 done" in r.getMessage() for r in caplog.records)  # log emitted


# ─────────────────────────── GET /internal/sync/status ───────────────────────


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(status_router)
    return TestClient(app)


def test_status_endpoint_reports_a_registered_trackers_progress():
    tracker = sync_status.register_tracker("test-slice")
    tracker.start(limit=5)
    tracker(SyncProgress(2, 5, "AAA", SyncOutcome.OK))

    resp = _client().get("/internal/sync/status")

    assert resp.status_code == 200
    by_name = {row["name"]: row for row in resp.json()}
    assert "test-slice" in by_name
    row = by_name["test-slice"]
    assert row["state"] == "running"
    assert (row["done"], row["total"], row["ok"]) == (2, 5, 1)
    assert row["last_symbol"] == "AAA"


def test_register_tracker_is_idempotent_by_name():
    first = sync_status.register_tracker("dup")
    second = sync_status.register_tracker("dup")
    assert first is second  # the live tracker is reused, not replaced
