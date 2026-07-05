"""Tests for the batch sync wall-clock backstop (``app.sync.runtime``).

Offline and fast. The timeout path's default action is a hard ``os._exit`` — untestable without
killing the runner — so ``run_with_timeout`` takes an injectable ``on_timeout``; the hang test
passes a recorder instead, and releases the blocked worker afterwards so no thread lingers.
"""

import threading

import pytest

from app.sync.runtime import max_runtime_seconds, run_with_timeout


def test_returns_normally_when_the_work_completes_in_time():
    ran = []
    run_with_timeout(lambda: ran.append("done"), timeout_s=5)
    assert ran == ["done"]


def test_reraises_whatever_the_work_raised():
    def boom():
        raise RuntimeError("yahoo blocked")

    with pytest.raises(RuntimeError, match="yahoo blocked"):
        run_with_timeout(boom, timeout_s=5)


def test_invokes_on_timeout_when_the_work_does_not_finish():
    release = threading.Event()
    fired: list[int] = []

    def hang():
        release.wait(5)  # blocks until released (or a safety cap) — simulates a stuck call

    # timeout_s=0: the join returns at once while the worker is still blocked, so the timeout
    # path fires instead of the (process-killing) default.
    run_with_timeout(hang, timeout_s=0, on_timeout=lambda t: fired.append(t))

    assert fired == [0]
    release.set()  # let the daemon worker unwind so nothing lingers past the test


def test_max_runtime_defaults_to_two_hours(monkeypatch):
    monkeypatch.delenv("SYNC_MAX_RUNTIME_S", raising=False)
    assert max_runtime_seconds() == 7200


def test_max_runtime_override_is_read_from_the_env(monkeypatch):
    monkeypatch.setenv("SYNC_MAX_RUNTIME_S", "600")
    assert max_runtime_seconds() == 600


def test_max_runtime_rejects_nonpositive_and_nonnumeric_values(monkeypatch):
    monkeypatch.setenv("SYNC_MAX_RUNTIME_S", "0")
    assert max_runtime_seconds() == 7200
    monkeypatch.setenv("SYNC_MAX_RUNTIME_S", "nope")
    assert max_runtime_seconds() == 7200
