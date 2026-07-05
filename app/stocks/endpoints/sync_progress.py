"""Heartbeat progress logging for the cron sync sweeps — the concrete ``ProgressReporter``.

``app.stocks.progress.ProgressReporter`` is the abstraction the sync use cases report through;
this is its production implementation, sitting beside ``background_sync`` as shared machinery for
the cron runners. ``HeartbeatReporter`` logs a sweep's progress on a fixed wall-clock interval
(default 5s) from a background daemon thread, so a long ECS sync task shows steady

    quarterly-earnings sync: 480/2800 (17%) | refreshed=470 failed=10 | 95s elapsed | ~7m left

lines in CloudWatch. Two properties matter:

- It's a **context manager**: the heartbeat thread runs only for the ``with`` block and a final
  summary line is logged on exit, so a runner just wraps its ``use_case.execute(...)`` in it.
- It ticks on a **wall clock**, not per item — so it keeps emitting even while a single Yahoo
  call stalls. A wedged sweep then shows a *frozen* counter ("still 47/2800") rather than going
  silent, which is exactly how you spot a hung run before the timeout backstop reaps it.

Threads + logging live here (the outer/infra edge), never in the use case — the use case only
ever sees the framework-free ``ProgressReporter`` interface.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from types import TracebackType
from typing import Callable

_DEFAULT_INTERVAL_S = 5.0
_MIN_INTERVAL_S = 0.5  # floor so a mis-set env can't turn the heartbeat into a busy-loop


def progress_interval_seconds() -> float:
    """The heartbeat cadence in seconds, from ``SYNC_PROGRESS_INTERVAL_S`` (default 5).

    Read here in the composition root (like the other cron env knobs). A non-numeric or
    non-positive value falls back to the default; anything below the busy-loop floor is raised to
    it.
    """
    raw = os.getenv("SYNC_PROGRESS_INTERVAL_S")
    if raw is None:
        return _DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_INTERVAL_S
    if value <= 0:
        return _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, value)


class HeartbeatReporter:
    """A ``ProgressReporter`` that logs "done/total (pct)" every ``interval_s`` on a daemon thread.

    Used as a context manager around a sweep: ``__enter__`` starts the heartbeat thread and
    ``__exit__`` stops it and logs a final line. The sweep calls ``start(total)`` once the work
    size is known and ``advance(ok=...)`` per item; the thread reads those counters under a lock
    and logs a snapshot each tick. ``now`` is injectable so tests get a deterministic clock.
    """

    def __init__(
        self,
        label: str,
        logger: logging.Logger,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._label = label
        self._logger = logger
        self._interval = max(_MIN_INTERVAL_S, interval_s)
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._total = 0
        self._done = 0
        self._failed = 0
        self._started_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── ProgressReporter ──────────────────────────────────────────────────────────────────
    def start(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._started_at = self._now()
        self._logger.info("%s: 0/%d (0%%) | starting", self._label, total)

    def advance(self, *, ok: bool = True) -> None:
        with self._lock:
            self._done += 1
            if not ok:
                self._failed += 1

    # ── context manager: run the heartbeat thread for the with-block ──────────────────────
    def __enter__(self) -> "HeartbeatReporter":
        self._thread = threading.Thread(
            target=self._run, name=f"{self._label}-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
        # A final line so the last state is always logged, even if the run finished mid-interval.
        self._log_snapshot(final=True)

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        # Emit on each interval until stopped. Event.wait returns True when the stop flag is set
        # (clean exit) and False on timeout (emit a heartbeat) — no drift-prone sleep loop.
        while not self._stop.wait(self._interval):
            self._log_snapshot(final=False)

    def _log_snapshot(self, *, final: bool) -> None:
        with self._lock:
            total = self._total
            done = self._done
            failed = self._failed
            started = self._started_at
        if total <= 0 or started is None:
            return  # start() hasn't run yet — nothing meaningful to report
        pct = int(done * 100 / total) if total else 0
        elapsed = self._now() - started
        refreshed = done - failed
        remaining = max(0, total - done)
        tail = ""
        # An ETA only while the run is live and has made progress (a final line is "done", and
        # dividing by zero done items is meaningless).
        if not final and done > 0 and remaining > 0:
            eta = int(elapsed / done * remaining)
            tail = f" | ~{_human_duration(eta)} left"
        elif final:
            tail = " | done"
        self._logger.info(
            "%s: %d/%d (%d%%) | refreshed=%d failed=%d | %s elapsed%s",
            self._label,
            done,
            total,
            pct,
            refreshed,
            failed,
            _human_duration(int(elapsed)),
            tail,
        )


def _human_duration(seconds: int) -> str:
    """A compact ``Xm``/``Ys`` rendering for the ETA (e.g. ``430`` -> ``7m``); seconds under a
    minute stay in seconds."""
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"
