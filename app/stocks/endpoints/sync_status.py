"""In-process progress/status tracking for the fire-and-forget cron sweeps.

The read-side companion to the per-stock ``on_progress`` channel: the same ticks that drive
the log heartbeats (``background_sync.logging_progress_reporter``) also update a per-slice
``SyncStatusTracker`` here, which ``GET /internal/sync/status`` serializes — so a poller can see
where a minutes-long sweep is *right now* without grepping the logs.

In-process and per-slice, matching the single-flight lock's per-process caveat and today's
single always-on task: the state lives in module memory, resets on restart, and is only correct
while there is one container. If the service ever scales past one task, this moves to a shared
store (Redis, or a ``sync_runs`` table) — the ``SyncStatusTracker`` interface stays the same, so
only this module changes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from app.stocks.sync_progress import SyncOutcome, SyncProgress


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SyncStatusSnapshot:
    """A consistent point-in-time view of one slice's sweep, safe to serialize.

    While ``state`` is ``"running"`` the counts are of the sweep in flight; once it is
    ``"idle"`` they describe the last completed run (until the next one starts). ``started_at``
    is ``None`` until a slice's first-ever run.
    """

    name: str
    state: str  # "idle" | "running"
    limit: int | None
    total: int | None
    done: int
    ok: int
    failed: int
    skipped: int
    last_symbol: str | None
    started_at: str | None
    finished_at: str | None
    last_error: str | None


class SyncStatusTracker:
    """Thread-safe, in-process progress of one slice's sweep.

    It is itself a ``ProgressReporter`` (``__call__``), so the runner fans the per-stock ticks
    into it; the runner brackets the run with ``start`` / ``finish`` (via ``track_run``). The
    sweep updates it on a background thread while the status endpoint reads it on a request
    thread, so every field access is under the lock.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._state = "idle"
        self._limit: int | None = None
        self._total: int | None = None
        self._done = 0
        self._ok = 0
        self._failed = 0
        self._skipped = 0
        self._last_symbol: str | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._last_error: str | None = None

    def start(self, limit: int | None) -> None:
        """Mark a fresh run started, resetting the per-run counters."""
        with self._lock:
            self._state = "running"
            self._limit = limit
            self._total = None
            self._done = self._ok = self._failed = self._skipped = 0
            self._last_symbol = None
            self._started_at = _utcnow_iso()
            self._finished_at = None
            self._last_error = None

    def __call__(self, progress: SyncProgress) -> None:
        """Absorb one per-stock tick (the ``ProgressReporter`` contract)."""
        with self._lock:
            self._total = progress.total
            self._done = progress.done
            self._last_symbol = progress.symbol
            if progress.outcome is SyncOutcome.OK:
                self._ok += 1
            elif progress.outcome is SyncOutcome.FAILED:
                self._failed += 1
            else:
                self._skipped += 1

    def finish(self, error: str | None = None) -> None:
        """Mark the run done (``error`` is the exception repr when it crashed)."""
        with self._lock:
            self._state = "idle"
            self._finished_at = _utcnow_iso()
            self._last_error = error

    def snapshot(self) -> SyncStatusSnapshot:
        with self._lock:
            return SyncStatusSnapshot(
                name=self._name,
                state=self._state,
                limit=self._limit,
                total=self._total,
                done=self._done,
                ok=self._ok,
                failed=self._failed,
                skipped=self._skipped,
                last_symbol=self._last_symbol,
                started_at=self._started_at,
                finished_at=self._finished_at,
                last_error=self._last_error,
            )


# Registry so one status endpoint can report every slice without importing each cron module.
# Each cron module registers its tracker at import; idempotent by name so repeat imports (tests)
# reuse the same tracker rather than dropping the live one.
_registry: dict[str, SyncStatusTracker] = {}
_registry_lock = threading.Lock()


def register_tracker(name: str) -> SyncStatusTracker:
    """Return the tracker for ``name``, creating it on first use."""
    with _registry_lock:
        tracker = _registry.get(name)
        if tracker is None:
            tracker = SyncStatusTracker(name)
            _registry[name] = tracker
        return tracker


def all_snapshots() -> list[SyncStatusSnapshot]:
    """A snapshot of every registered slice, ordered by registration (i.e. import order)."""
    with _registry_lock:
        trackers = list(_registry.values())
    return [tracker.snapshot() for tracker in trackers]


@contextmanager
def track_run(tracker: SyncStatusTracker, limit: int | None) -> Iterator[None]:
    """Bracket a sweep: mark the tracker started, and finished on the way out — recording the
    exception repr if it raised, then re-raising so the shared guard still logs it."""
    tracker.start(limit)
    try:
        yield
    except BaseException as exc:  # record every failure mode, then re-raise
        tracker.finish(error=repr(exc))
        raise
    else:
        tracker.finish()
