"""Tests for the shared cron-sync background helper (app/stocks/endpoints/background_sync.py).

Drives ``trigger_sync`` directly with fake runners and a bare ``Response``, so it covers the
threading contract the three cron endpoints share: single-flight guarding, the 202/200
outcome, and — the part awkward to reach through an endpoint — that a runner that raises
still releases the guard and never lets the exception escape the thread.

Determinism: ``trigger_sync`` holds the lock until the daemon thread releases it, so
re-acquiring the lock is a "sweep done" barrier — no sleeps.
"""

import logging
import threading

from fastapi import Response

from app.stocks.endpoints import background_sync
from app.stocks.sync_progress import SyncOutcome, SyncProgress


class _FakeRunner:
    """Records the limit it was called with; optionally raises to exercise the failure path."""

    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[int] = []
        self._boom = boom

    def __call__(self, limit: int) -> str:
        self.calls.append(limit)
        if self._boom:
            raise RuntimeError("simulated sweep failure")
        return "ok"


def _drain(lock: threading.Lock) -> None:
    assert lock.acquire(timeout=2), "background sweep did not finish in time"
    lock.release()


def test_starts_the_sweep_and_reports_accepted():
    lock = threading.Lock()
    runner = _FakeRunner()
    result = background_sync.trigger_sync(lock, runner, 50, Response(), label="test")
    assert result.status == "accepted"
    assert result.limit == 50
    _drain(lock)
    assert runner.calls == [50]  # the runner ran, with the given limit


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    lock = threading.Lock()
    runner = _FakeRunner()
    assert lock.acquire(blocking=False)  # simulate a sweep already in flight
    try:
        response = Response()
        response.status_code = 202  # the route's default, which the no-op path overrides
        result = background_sync.trigger_sync(lock, runner, 50, response, label="test")
        assert result.status == "already_running"
        assert response.status_code == 200  # overridden to 200 for the no-op
        assert runner.calls == []  # nothing started
    finally:
        lock.release()


def test_a_runner_error_is_swallowed_and_the_guard_released():
    lock = threading.Lock()
    runner = _FakeRunner(boom=True)
    result = background_sync.trigger_sync(lock, runner, 10, Response(), label="test")
    assert result.status == "accepted"
    # The thread must release the guard even though the runner raised — otherwise this
    # barrier would hang — and the exception must not have propagated out of the thread.
    _drain(lock)
    assert runner.calls == [10]


# ───────────────────────── logging_progress_reporter ─────────────────────────


def test_progress_reporter_heartbeats_on_interval_and_warns_on_failure(caplog):
    # every=2, so ticks 1 (first), 2 (interval), and 4 (last) heartbeat at INFO; tick 3 is
    # silent. A FAILED tick also logs a WARNING naming the symbol and reason.
    report = background_sync.logging_progress_reporter("test sync", every=2)
    with caplog.at_level(logging.INFO, logger=background_sync.__name__):
        report(SyncProgress(1, 4, "AAA", SyncOutcome.OK))
        report(SyncProgress(2, 4, "BBB", SyncOutcome.FAILED, "unavailable"))
        report(SyncProgress(3, 4, "CCC", SyncOutcome.OK))
        report(SyncProgress(4, 4, "DDD", SyncOutcome.OK))

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(infos) == 3  # ticks 1, 2, 4 — not 3
    assert len(warnings) == 1
    assert "BBB" in warnings[0].getMessage()
    assert "unavailable" in warnings[0].getMessage()
    # The final heartbeat carries the running tallies accumulated across the whole run.
    assert "ok=3 failed=1 skipped=0" in infos[-1].getMessage()


def test_progress_reporter_counts_skipped_separately_from_failed(caplog):
    report = background_sync.logging_progress_reporter("test sync", every=1)
    with caplog.at_level(logging.INFO, logger=background_sync.__name__):
        report(SyncProgress(1, 2, "AAA", SyncOutcome.SKIPPED, "unclassified"))
        report(SyncProgress(2, 2, "BBB", SyncOutcome.OK))

    # SKIPPED is a deliberate no-op, not a failure: no WARNING, and its own tally column.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert "ok=1 failed=0 skipped=1" in infos[-1].getMessage()
