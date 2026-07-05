"""Tests for the cron sweep progress reporters.

Offline and deterministic: the ``HeartbeatReporter`` is driven with a fake clock and a
capturing logger and its snapshot is rendered by calling ``_log_snapshot`` directly, so nothing
depends on real time or thread scheduling. One lightweight test does exercise the context-manager
lifecycle (a real daemon thread) but with an interval long enough that the heartbeat never ticks
on its own — only the ``start`` and final lines are asserted, so it stays deterministic.
"""

from app.stocks.endpoints.sync_progress import (
    HeartbeatReporter,
    progress_interval_seconds,
)
from app.stocks.progress import NullProgress


class _FakeLogger:
    """Records formatted log messages (``msg % args``), like the fields a real logger renders."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg, *args) -> None:
        self.messages.append(msg % args if args else msg)

    def error(self, msg, *args) -> None:
        self.messages.append(msg % args if args else msg)


class _Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ───────────────────────────── NullProgress ─────────────────────────────


def test_null_progress_is_a_silent_noop():
    reporter = NullProgress()
    # The default reporter must accept the full protocol without doing (or raising) anything.
    reporter.start(10)
    reporter.advance()
    reporter.advance(ok=False)


# ───────────────────────────── HeartbeatReporter snapshot ─────────────────────────────


def test_start_logs_a_zero_line_and_sets_the_denominator():
    log = _FakeLogger()
    reporter = HeartbeatReporter("qtr sync", log, interval_s=5, now=_Clock())

    reporter.start(2800)

    assert log.messages == ["qtr sync: 0/2800 (0%) | starting"]


def test_snapshot_reports_count_percent_split_and_eta():
    clock = _Clock()
    log = _FakeLogger()
    reporter = HeartbeatReporter("qtr sync", log, interval_s=5, now=clock)

    clock.t = 100.0
    reporter.start(10)
    reporter.advance()
    reporter.advance()
    reporter.advance()
    reporter.advance(ok=False)
    clock.t = 110.0  # 10s elapsed, 4 of 10 done (1 failed)

    reporter._log_snapshot(final=False)

    msg = log.messages[-1]
    assert "qtr sync: 4/10 (40%)" in msg
    assert "refreshed=3 failed=1" in msg
    assert "10s elapsed" in msg
    # ETA = elapsed/done * remaining = 10/4 * 6 = 15s.
    assert "~15s left" in msg


def test_final_snapshot_says_done_and_drops_the_eta():
    clock = _Clock()
    log = _FakeLogger()
    reporter = HeartbeatReporter("qtr sync", log, interval_s=5, now=clock)

    clock.t = 0.0
    reporter.start(2)
    reporter.advance()
    clock.t = 4.0

    reporter._log_snapshot(final=True)

    msg = log.messages[-1]
    assert "1/2 (50%)" in msg
    assert "| done" in msg
    assert "left" not in msg  # no ETA on the final line


def test_snapshot_is_silent_before_start():
    log = _FakeLogger()
    reporter = HeartbeatReporter("qtr sync", log, interval_s=5, now=_Clock())

    reporter._log_snapshot(final=False)  # start() never called — total is 0

    assert log.messages == []


def test_eta_is_rendered_in_minutes_for_long_runs():
    clock = _Clock()
    log = _FakeLogger()
    reporter = HeartbeatReporter("uni sync", log, interval_s=5, now=clock)

    clock.t = 0.0
    reporter.start(100)
    reporter.advance()  # 1 of 100 done...
    clock.t = 10.0  # ...in 10s -> 99 remaining * 10s = 990s ~ 16m

    reporter._log_snapshot(final=False)

    assert "~16m left" in log.messages[-1]


# ───────────────────────────── HeartbeatReporter lifecycle ─────────────────────────────


def test_context_manager_logs_start_and_final_lines():
    clock = _Clock()
    log = _FakeLogger()
    # A long interval means the background thread never ticks on its own before __exit__ stops
    # it — so the only lines are the start line and the final "done" line (deterministic).
    with HeartbeatReporter("rec sync", log, interval_s=100, now=clock) as reporter:
        clock.t = 1.0
        reporter.start(4)
        reporter.advance()
        reporter.advance()
        clock.t = 5.0

    assert any("| starting" in m for m in log.messages)
    assert any("2/4 (50%)" in m and "| done" in m for m in log.messages)


# ───────────────────────────── progress_interval_seconds ─────────────────────────────


def test_interval_defaults_to_five_seconds(monkeypatch):
    monkeypatch.delenv("SYNC_PROGRESS_INTERVAL_S", raising=False)
    assert progress_interval_seconds() == 5.0


def test_interval_override_is_read_from_the_env(monkeypatch):
    monkeypatch.setenv("SYNC_PROGRESS_INTERVAL_S", "10")
    assert progress_interval_seconds() == 10.0


def test_interval_rejects_nonpositive_and_nonnumeric_values(monkeypatch):
    monkeypatch.setenv("SYNC_PROGRESS_INTERVAL_S", "0")
    assert progress_interval_seconds() == 5.0
    monkeypatch.setenv("SYNC_PROGRESS_INTERVAL_S", "junk")
    assert progress_interval_seconds() == 5.0


def test_interval_is_floored_to_avoid_a_busy_loop(monkeypatch):
    monkeypatch.setenv("SYNC_PROGRESS_INTERVAL_S", "0.1")
    assert progress_interval_seconds() == 0.5
